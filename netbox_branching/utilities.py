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
from django.utils.translation import gettext as _
from netbox.plugins import get_plugin_config
from netbox.utils import register_request_processor

from .constants import (
    _FILE_NOT_FOUND_EXCEPTIONS,
    BRANCH_HEADER,
    COOKIE_NAME,
    EXEMPT_MODELS,
    EXEMPT_PATHS,
    INCLUDE_MODELS,
    QUERY_PARAM,
)
from .contextvars import active_branch

logger = logging.getLogger(__name__)

# Thread-local storage for tracking branch connection aliases (matches Django's approach)
# Note: Aliases are tracked once and never removed, matching Django's pattern where
# DATABASES.keys() is static. Memory overhead is negligible (string references only).
_branch_connections_tracker = Local(thread_critical=False)

__all__ = (
    'ActiveBranchContextManager',
    'BranchActionIndicator',
    'ChangeSummary',
    'DynamicSchemaDict',
    'ListHandler',
    'activate_branch',
    'clear_mptt_fields',
    'close_old_branch_connections',
    'deactivate_branch',
    'full_clean_with_file_check',
    'get_active_branch',
    'get_branchable_object_types',
    'get_sql_results',
    'get_tables_to_replicate',
    'is_api_request',
    'record_applied_change',
    'register_branching_resolver',
    'register_objectchange_field_migrator',
    'resolve_changes_summary',
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
        if type(item) is str and item.startswith('schema_') and (schema := item.removeprefix('schema_')):
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
    try:
        yield
    finally:
        active_branch.reset(token)


@contextmanager
def deactivate_branch():
    """
    A context manager for temporarily deactivating the active Branch (if any). This is a
    convenience function for `activate_branch(None)`.
    """
    token = active_branch.set(None)
    try:
        yield
    finally:
        active_branch.reset(token)


def get_branchable_object_types():
    """
    Return all object types which are branch-aware; i.e. those which support change logging.
    """
    from core.models import ObjectType

    return ObjectType.objects.with_feature('branching')


_branching_resolvers = []
_objectchange_field_migrators = []


def register_objectchange_field_migrator(migrator):
    """
    Register a callable that rewrites field-name keys in an ObjectChange data
    dict before the dict is applied or compared.

    Signature: ``migrator(model, data) -> dict | None``.  Returning a dict
    replaces ``data`` for subsequent processing; returning ``None`` defers to
    the next registered migrator.  The first non-``None`` return wins.

    Internal extension point.  Not part of the public plugin API and subject
    to change without notice; external plugins should not rely on it.
    """
    if not callable(migrator):
        raise TypeError('ObjectChange field migrator must be callable')
    _objectchange_field_migrators.append(migrator)


def resolve_objectchange_field_migration(model, data):
    """
    Apply the first registered migrator that claims ``model`` and return the
    (possibly translated) ``data`` dict.  When no migrator claims the model,
    ``data`` is returned unchanged.

    Internal helper; not part of the public API.
    """
    if data is None or model is None:
        return data
    for migrator in _objectchange_field_migrators:
        try:
            result = migrator(model, data)
        except Exception:
            logger.exception(
                'objectchange field migrator %r raised; treating as None', migrator
            )
            continue
        if result is not None:
            return result
    return data


def register_branching_resolver(resolver):
    """
    Register a callable that determines branching support for a model.

    Resolvers run after the explicit ``INCLUDE_MODELS`` check and before the
    default ``ChangeLoggingMixin`` heuristic.  Used by plugins whose models
    don't follow the standard NetBox change-logging mixin pattern but still
    need to be routed to the active branch's schema — e.g. dynamically-
    generated M2M through models that share a parent's branchable status
    but are themselves plain ``models.Model`` subclasses.

    Signature: ``resolver(model) -> bool | None``
        True  — model is branchable, route to active branch
        False — model is not branchable, route to main
        None  — defer to next resolver / default heuristic

    The first non-``None`` return wins.
    """
    if not callable(resolver):
        raise TypeError('Branching resolver must be callable')
    _branching_resolvers.append(resolver)


def supports_branching(model):
    """
    Returns True if branching is supported for the given model; otherwise False.
    """
    from django.apps import apps as live_apps
    from netbox.models.features import ChangeLoggingMixin

    label = f'{model._meta.app_label}.{model._meta.model_name}'
    wildcard_label = f'{model._meta.app_label}.*'

    # Check for explicitly supported models
    if label in INCLUDE_MODELS:
        return True

    # Plugin-registered resolvers — let plugins extend branching to their
    # non-ChangeLoggingMixin models when appropriate.
    for resolver in _branching_resolvers:
        try:
            result = resolver(model)
        except Exception:
            logger.exception('branching resolver %r raised; treating as None', resolver)
            continue
        if result is not None:
            if not result:
                return False
            # True: still apply the exempt-models filter below.
            break
    else:
        # RunPython data migrations receive historical models from the migration's
        # StateApps. Those don't inherit ChangeLoggingMixin even when the live
        # model does, so the issubclass() check would mis-classify them and the
        # router would send branch-aware queries to main. Resolve to the live
        # registry for an accurate class-hierarchy check.
        try:
            resolved_model = live_apps.get_model(model._meta.app_label, model._meta.model_name)
        except LookupError:
            resolved_model = model

        # Exclude models which do not support change logging
        if not issubclass(resolved_model, ChangeLoggingMixin):
            return False

    # TODO: Make this more efficient
    # Check for exempted models
    exempt_models = [
        *EXEMPT_MODELS,
        *get_plugin_config('netbox_branching', 'exempt_models', []),
    ]
    return label not in exempt_models and wildcard_label not in exempt_models


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


def full_clean_with_file_check(instance, logger):
    """
    Calls instance.full_clean(), suppressing _FILE_NOT_FOUND_EXCEPTIONS for genuinely missing
    files. For BotocoreClientError, only 403/404 responses are suppressed — S3 can return 403
    for missing objects when the bucket policy denies s3:ListBucket.
    """
    try:
        instance.full_clean()
    except _FILE_NOT_FOUND_EXCEPTIONS as e:
        if hasattr(e, 'response'):
            status = e.response.get('ResponseMetadata', {}).get('HTTPStatusCode')
            if status not in (403, 404):
                raise
        logger.warning(f'Ignoring missing file: {e}')


def clear_mptt_fields(instance):
    """
    Reset the MPTT-managed tree fields (lft/rght/level/tree_id) on an instance to None
    so that MPTT will recompute them when the instance is next saved via Model.save().
    """
    opts = instance._mptt_meta
    for attr in (opts.left_attr, opts.right_attr, opts.level_attr, opts.tree_id_attr):
        setattr(instance, attr, None)


class _DeletedKey:
    """
    Sentinel marking a dict key that should be removed (rather than set to None)
    during a deep merge. See ``diff_for_merge``.
    """
    __slots__ = ()

    def __repr__(self):
        return '<DELETED>'


# Singleton sentinel instance, compared by identity.
DELETED = _DeletedKey()


def diff_for_merge(source, destination):
    """
    Compute the partial-update payload that transforms ``source`` into ``destination``
    when deep-merged onto a target via ``update_object`` / ``_deep_merge_dict``.

    This mirrors the "added" side of NetBox's ``deep_compare_dict`` — only differing
    keys are returned, and nested dicts yield only their changed keys so that keys the
    branch never touched are preserved on the target schema (#588) — with one critical
    difference: a key present in ``source`` but absent from ``destination`` *within a
    nested dict* is represented with the ``DELETED`` sentinel instead of being dropped.

    ``deep_compare_dict`` reads nested values with ``dict.get()``, so it cannot tell
    "key removed" apart from "key set to None" and silently omits removals. Replaying
    that diff would leave stale keys behind inside JSON fields (e.g. a key deleted from
    a JSON custom field's value, or from ``local_context_data``) on the main schema
    after a merge or revert. Tracking removals explicitly lets the merge drop them. (#592)

    Top-level removals are represented with the value ``None`` (matching prior behaviour),
    since those flow through ``setattr`` rather than a deep merge.
    """
    return _diff_for_merge(source, destination, top_level=True)


def _diff_for_merge(source, destination, top_level=False):
    delta = {}
    for key in sorted(source.keys() | destination.keys()):
        src_val = source.get(key)
        dst_val = destination.get(key)
        if src_val == dst_val:
            continue
        if isinstance(src_val, dict) and isinstance(dst_val, dict):
            # Recurse; unequal dicts always yield at least one nested change.
            delta[key] = _diff_for_merge(src_val, dst_val)
        elif key not in destination and not top_level:
            delta[key] = DELETED
        else:
            delta[key] = dst_val
    return delta


def _strip_deleted(value):
    """
    Return a copy of ``value`` with any ``DELETED``-sentinel keys removed (recursively).
    Used when a partial JSON update has no existing dict to merge into, so deletion
    markers must simply be dropped rather than written to the field.
    """
    if not isinstance(value, dict):
        return value
    return {k: _strip_deleted(v) for k, v in value.items() if v is not DELETED}


def _deep_merge_dict(target, source):
    """
    Recursively merge ``source`` into ``target`` in place. Nested dicts are
    merged key-by-key; the ``DELETED`` sentinel removes the key from ``target``;
    all other values are replaced.
    """
    for key, value in source.items():
        if value is DELETED:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge_dict(target[key], value)
        else:
            target[key] = value
    return target


def update_object(instance, data, using):
    """
    Set an attribute on an object depending on the type of model field.

    ``data`` is passed through any registered ObjectChange field migrators
    once at the top of the function, so stale field-name keys can be
    rewritten to the model's current attribute names before the apply loop
    runs.  When no migrator claims the model, ``data`` is used as-is.

    The migrator hook is internal and subject to change; external plugins
    should not rely on it.
    """
    # Avoid AppRegistryNotReady exception
    from taggit.managers import TaggableManager
    logger = logging.getLogger('netbox_branching.utilities.update_object')
    instance.snapshot()
    m2m_assignments = {}

    data = resolve_objectchange_field_migration(type(instance), data)

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
        elif isinstance(value, dict):
            # JSON fields like custom_field_data and local_context_data arrive as partial
            # dicts (via diff_for_merge) containing only the keys the change touched, with
            # removed keys carrying the DELETED sentinel. Merge into the existing dict so
            # keys the branch never modified are preserved on the target schema (#588) and
            # removed keys are dropped (#592). If there's no dict to merge into, materialize
            # the partial dict, discarding deletion markers so the sentinel never reaches
            # the field.
            current = getattr(instance, attr, None)
            if isinstance(current, dict):
                _deep_merge_dict(current, value)
            else:
                setattr(instance, attr, _strip_deleted(value))
        else:
            setattr(instance, attr, value)

    full_clean_with_file_check(instance, logger)
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
    if QUERY_PARAM in request.GET:
        if schema_id := request.GET.get(QUERY_PARAM):
            branch = Branch.objects.get(schema_id=schema_id)
            if branch.ready:
                if (
                    schema_id != request.COOKIES.get(COOKIE_NAME)
                    and not getattr(request, '_branch_activation_notified', False)
                ):
                    messages.success(request, _("Activated branch {branch}").format(branch=branch))
                    request._branch_activation_notified = True
                return branch
            if not getattr(request, '_branch_activation_notified', False):
                messages.error(request, _("Branch {branch} is not ready for use (status: {status})").format(
                    branch=branch, status=branch.status
                ))
                request._branch_activation_notified = True
            return None
        if not getattr(request, '_branch_activation_notified', False):
            messages.success(request, _("Deactivated branch"))
            request._branch_activation_notified = True
        request.COOKIES.pop(COOKIE_NAME, None)  # Delete cookie if set
        return None

    # Branch set by cookie
    if schema_id := request.COOKIES.get(COOKIE_NAME):
        try:
            branch = Branch.objects.get(schema_id=schema_id)
            if branch.ready:
                return branch
        except ObjectDoesNotExist:
            pass
    return None


def resolve_changes_summary(stored):
    """
    Convert a stored changes summary (with 'app_label.model' string keys) into a display-ready
    dict keyed by ContentType objects. Used by views to render job report data.
    """
    from django.contrib.contenttypes.models import ContentType

    def _resolve(d):
        result = {}
        for key, count in d.items():
            app_label, model = key.split('.', 1)
            try:
                ct = ContentType.objects.get_by_natural_key(app_label, model)
                result[ct] = count
            except ContentType.DoesNotExist:
                pass
        return result

    return {
        'creates': _resolve(stored.get('creates', {})),
        'creates_total': stored.get('creates_total', 0),
        'updates': _resolve(stored.get('updates', {})),
        'updates_total': stored.get('updates_total', 0),
        'deletes': _resolve(stored.get('deletes', {})),
        'deletes_total': stored.get('deletes_total', 0),
    }


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
    Activate a branch if indicated by the request (except for exempt paths).
    """
    if request and request.path not in EXEMPT_PATHS and (branch := get_active_branch(request)):
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
