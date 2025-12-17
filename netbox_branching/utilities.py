import datetime
import logging
from collections import namedtuple
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from functools import cached_property

from asgiref.local import Local
from django.contrib import messages
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from django.db import connections
from django.db.models import ForeignKey, ManyToManyField
from django.http import HttpResponseBadRequest
from django.urls import reverse

from netbox.plugins import get_plugin_config
from netbox.utils import register_request_processor
from .constants import BRANCH_HEADER, COOKIE_NAME, EXEMPT_MODELS, INCLUDE_MODELS, QUERY_PARAM
from .contextvars import active_branch

# Thread-local storage for tracking branch connection aliases (matches Django's approach)
# Note: Aliases are tracked once and never removed, matching Django's pattern where
# DATABASES.keys() is static. Memory overhead is negligible (string references only).
_branch_connections_tracker = Local(thread_critical=False)

__all__ = (
    'BranchActionIndicator',
    'ChangeSummary',
    'DynamicSchemaDict',
    'ListHandler',
    'ActiveBranchContextManager',
    'activate_branch',
    'close_old_branch_connections',
    'deactivate_branch',
    'get_active_branch',
    'get_branchable_object_types',
    'get_sql_results',
    'get_tables_to_replicate',
    'is_api_request',
    'record_applied_change',
    'supports_branching',
    'track_branch_connection',
    'update_object',
)


def _get_tracked_branch_aliases():
    """Get set of tracked branch aliases for current thread."""
    if not hasattr(_branch_connections_tracker, 'aliases'):
        _branch_connections_tracker.aliases = set()
    return _branch_connections_tracker.aliases


def track_branch_connection(alias):
    """Register a branch connection alias for cleanup tracking."""
    _get_tracked_branch_aliases().add(alias)


class DynamicSchemaDict(dict):
    """
    Behaves like a normal dictionary, except for keys beginning with "schema_". Any lookup for
    "schema_*" will return the default configuration extended to include the search_path option.
    """
    @cached_property
    def main_schema(self):
        return get_plugin_config('netbox_branching', 'main_schema')

    def __getitem__(self, item):
        if type(item) is str and item.startswith('schema_'):
            if schema := item.removeprefix('schema_'):
                track_branch_connection(item)

                default_config = super().__getitem__('default')
                return {
                    **default_config,
                    'OPTIONS': {
                        **default_config.get('OPTIONS', {}),
                        'options': f'-c search_path={schema},{self.main_schema}'
                    },
                }
        return super().__getitem__(item)

    def __contains__(self, item):
        if type(item) is str and item.startswith('schema_'):
            return True
        return super().__contains__(item)


def close_old_branch_connections(**kwargs):
    """
    Close branch database connections that have exceeded their maximum age.

    This function complements Django's close_old_connections() by handling
    dynamically-created branch connections. It tracks branch connection aliases
    in thread-local storage and closes them when they exceed CONN_MAX_AGE.

    Django's close_old_connections() only closes connections for database aliases
    found in DATABASES.keys(). Since branch aliases are generated dynamically and
    not present in that iteration (to avoid test isolation issues), they would never
    be cleaned up, causing connection leaks.

    This function is connected to request_started and request_finished signals,
    matching Django's cleanup timing.
    """

    for alias in _get_tracked_branch_aliases():
        conn = connections[alias]
        conn.close_if_unusable_or_obsolete()


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


def supports_branching(model):
    """
    Returns True if branching is supported for the given model; otherwise False.
    """
    from netbox.models.features import ChangeLoggingMixin

    label = f'{model._meta.app_label}.{model._meta.model_name}'
    wildcard_label = f'{model._meta.app_label}.*'

    # Check for explicitly supported models
    if label in INCLUDE_MODELS:
        return True

    # Exclude models which do not support change logging
    if not issubclass(model, ChangeLoggingMixin):
        return False

    # TODO: Make this more efficient
    # Check for exempted models
    exempt_models = [
        *EXEMPT_MODELS,
        *get_plugin_config('netbox_branching', 'exempt_models', []),
    ]
    if label in exempt_models or wildcard_label in exempt_models:
        return False

    return True


def get_tables_to_replicate():
    """
    Return an ordered list of database tables to replicate when provisioning a new schema.
    """
    tables = set()

    branch_aware_models = [
        ot.model_class() for ot in get_branchable_object_types()
        if ot.model_class() is not None
    ]
    for model in branch_aware_models:

        # Capture the model's table
        tables.add(model._meta.db_table)

        # Capture any M2M fields which reference other replicated models
        for m2m_field in model._meta.local_many_to_many:
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
    logger = logging.getLogger('netbox_branching.utilities.update_object')
    instance.snapshot()
    m2m_assignments = {}

    for attr, value in data.items():
        # Account for custom field data
        if attr == 'custom_fields':
            attr = 'custom_field_data'

        try:
            model_field = instance._meta.get_field(attr)
            field_cls = model_field.__class__
        except FieldDoesNotExist:
            field_cls = None

        if field_cls and issubclass(field_cls, ForeignKey):
            # Direct value assignment for ForeignKeys must be done by the field's concrete name
            setattr(instance, f'{attr}_id', value)
        elif field_cls and issubclass(field_cls, (ManyToManyField, TaggableManager)):
            # Use M2M manager for ManyToMany assignments
            m2m_manager = getattr(instance, attr)
            m2m_assignments[m2m_manager] = value
        else:
            setattr(instance, attr, value)

    try:
        instance.full_clean()
    except (FileNotFoundError) as e:
        # If a file was deleted later in this branch it will fail here
        # so we need to ignore it. We can assume the NetBox state is valid.
        logger.warning(f'Ignoring missing file: {e}')
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
        try:
            branch = Branch.objects.get(schema_id=schema_id)
            if branch.ready:
                return branch
        except ObjectDoesNotExist:
            pass


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


@dataclass
class BranchActionIndicator:
    """
    An indication of whether a particular branch action is permitted. If not, an explanatory message must be provided.
    """
    permitted: bool
    message: str = ''

    def __bool__(self):
        return self.permitted
