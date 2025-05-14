import datetime
import logging
from collections import defaultdict, namedtuple
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

from django.contrib import messages
from django.db.models import ForeignKey, ManyToManyField
from django.http import HttpResponseBadRequest
from django.urls import reverse

from netbox.plugins import get_plugin_config
from netbox.registry import registry
from netbox.utils import register_request_processor
from .choices import BranchStatusChoices
from .constants import BRANCH_HEADER, COOKIE_NAME, EXEMPT_MODELS, INCLUDE_MODELS, QUERY_PARAM
from .contextvars import active_branch

__all__ = (
    'ChangeSummary',
    'DynamicSchemaDict',
    'ListHandler',
    'ActiveBranchContextManager',
    'activate_branch',
    'deactivate_branch',
    'get_active_branch',
    'get_branchable_object_types',
    'get_sql_results',
    'get_tables_to_replicate',
    'is_api_request',
    'record_applied_change',
    'register_models',
    'update_object',
)


class DynamicSchemaDict(dict):
    """
    Behaves like a normal dictionary, except for keys beginning with "schema_". Any lookup for
    "schema_*" will return the default configuration extended to include the search_path option.
    """
    def __getitem__(self, item):
        if type(item) is str and item.startswith('schema_'):
            if schema := item.removeprefix('schema_'):
                default_config = super().__getitem__('default')
                return {
                    **default_config,
                    'OPTIONS': {
                        'options': f'-c search_path={schema},public'
                    }
                }
        return super().__getitem__(item)

    def __contains__(self, item):
        if type(item) is str and item.startswith('schema_'):
            return True
        return super().__contains__(item)


@contextmanager
def activate_branch(branch):
    """
    A context manager for activating a Branch.
    """
    token = active_branch.set(branch)

    yield

    active_branch.reset(token)


@contextmanager
def deactivate_branch():
    """
    A context manager for temporarily deactivating the active Branch (if any). This is a
    convenience function for `activate_branch(None)`.
    """
    token = active_branch.set(None)

    yield

    active_branch.reset(token)


def get_branchable_object_types():
    """
    Return all object types which are branch-aware; i.e. those which support change logging.
    """
    from core.models import ObjectType

    return ObjectType.objects.with_feature('branching')


def register_models():
    """
    Register all models which support branching in the NetBox registry.
    """
    # Compile a list of exempt models (those for which change logging may
    # be enabled, but branching is not supported)
    exempt_models = (
        *EXEMPT_MODELS,
        *get_plugin_config('netbox_branching', 'exempt_models'),
    )

    # Register all models which support change logging and are not exempt
    branching_models = defaultdict(list)
    for app_label, models in registry['model_features']['change_logging'].items():
        # Wildcard exclusion for all models in this app
        if f'{app_label}.*' in exempt_models:
            continue
        for model in models:
            if f'{app_label}.{model}' not in exempt_models:
                branching_models[app_label].append(model)

    # Register additional included models
    # TODO: Allow plugins to declare additional models?
    for label in INCLUDE_MODELS:
        app_label, model = label.split('.')
        branching_models[app_label].append(model)

    registry['model_features']['branching'] = dict(branching_models)


def get_tables_to_replicate():
    """
    Return an ordered list of database tables to replicate when provisioning a new schema.
    """
    tables = set()

    branch_aware_models = [
        ot.model_class() for ot in get_branchable_object_types()
    ]
    for model in branch_aware_models:

        # Capture the model's table
        tables.add(model._meta.db_table)

        # Capture any M2M fields which reference other replicated models
        for m2m_field in model._meta.local_many_to_many:
            if m2m_field.related_model in branch_aware_models:
                if hasattr(m2m_field, 'through'):
                    # Field is actually a manager
                    m2m_table = m2m_field.through._meta.db_table
                else:
                    m2m_table = m2m_field._get_m2m_db_table(model._meta)
                tables.add(m2m_table)

    return sorted(tables)


class ListHandler(logging.Handler):
    """
    A logging handler which appends log messages to list passed on initialization.
    """
    def __init__(self, *args, queue, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = queue

    def emit(self, record):
        self.queue.append(self.format(record))


@dataclass
class ChangeSummary:
    """
    A record indicating the number of changes which were made between a start and end time.
    """
    start: datetime.datetime
    end: datetime.datetime
    count: int


def update_object(instance, data, using):
    """
    Set an attribute on an object depending on the type of model field.
    """
    # Avoid AppRegistryNotReady exception
    from taggit.managers import TaggableManager
    instance.snapshot()
    m2m_assignments = {}

    for attr, value in data.items():
        # Account for custom field data
        if attr == 'custom_fields':
            attr = 'custom_field_data'

        model_field = instance._meta.get_field(attr)
        field_cls = model_field.__class__

        if issubclass(field_cls, ForeignKey):
            # Direct value assignment for ForeignKeys must be done by the field's concrete name
            setattr(instance, f'{attr}_id', value)
        elif issubclass(field_cls, (ManyToManyField, TaggableManager)):
            # Use M2M manager for ManyToMany assignments
            m2m_manager = getattr(instance, attr)
            m2m_assignments[m2m_manager] = value
        else:
            setattr(instance, attr, value)

    instance.full_clean()
    instance.save(using=using)
    for m2m_manager, value in m2m_assignments.items():
        m2m_manager.set(value)


def record_applied_change(instance, branch, **kwargs):
    """
    Create a new AppliedChange instance mapping an applied ObjectChange to its Branch.
    """
    from .models import AppliedChange

    AppliedChange.objects.update_or_create(change=instance, defaults={'branch': branch})


def is_api_request(request):
    """
    Returns True if the given request is a REST or GraphQL API request.
    """
    if not hasattr(request, 'path_info'):
        return False

    return request.path_info.startswith(reverse('api-root')) or request.path_info.startswith(reverse('graphql'))


def get_active_branch(request):
    """
    Return the active Branch (if any).
    """
    # The active Branch may be specified by HTTP header for REST & GraphQL API requests.
    from .models import Branch
    if is_api_request(request) and BRANCH_HEADER in request.headers:
        branch = Branch.objects.get(schema_id=request.headers.get(BRANCH_HEADER))
        if not branch.ready:
            return HttpResponseBadRequest(f"Branch {branch} is not ready for use (status: {branch.status})")
        return branch

    # Branch activated/deactivated by URL query parameter
    elif QUERY_PARAM in request.GET:
        if schema_id := request.GET.get(QUERY_PARAM):
            branch = Branch.objects.get(schema_id=schema_id)
            if branch.ready:
                messages.success(request, f"Activated branch {branch}")
                return branch
            else:
                messages.error(request, f"Branch {branch} is not ready for use (status: {branch.status})")
                return None
        else:
            messages.success(request, "Deactivated branch")
            request.COOKIES.pop(COOKIE_NAME, None)  # Delete cookie if set
            return None

    # Branch set by cookie
    elif schema_id := request.COOKIES.get(COOKIE_NAME):
        return Branch.objects.filter(schema_id=schema_id, status=BranchStatusChoices.READY).first()


def get_sql_results(cursor):
    """
    Return the results of the most recent SQL query as a list of named tuples.
    """
    Result = namedtuple("Result", [col[0] for col in cursor.description])
    return [
        Result(*row) for row in cursor.fetchall()
    ]


@register_request_processor
def ActiveBranchContextManager(request):
    """
    Activate a branch if indicated by the request.
    """
    if request and (branch := get_active_branch(request)):
        return activate_branch(branch)
    return nullcontext()
