import logging
import re

from django.apps import apps
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import IntegrityError
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from .choices import BranchMergeStrategyChoices
from .constants import PG_UNIQUE_VIOLATION
from .utilities import activate_branch, full_clean_with_file_check

# Recommendation message templates — separated from decision logic in get_merge_recommendations()
_REC_RENAME_WITH_FIELD = _l(
    'Rename the conflicting object (where %(field)s="%(value)s") in either the branch'
    ' or the main schema.'
)
_REC_RENAME_GENERIC = _l(
    'Rename the conflicting object in either the branch or the main schema'
    ' so the values no longer conflict.'
)
_REC_TRY_SQUASH_UNIQUE = _l(
    'Switch to the Squash merge strategy, which handles these types of conflicts better.'
)
_REC_FIX_FIELD = _l(
    'Fix the invalid value for field "%(field)s" on the affected object in the branch before retrying.'
)
_REC_FIX_GENERIC = _l(
    'Fix the invalid value on the affected object in the branch before retrying.'
)
_REC_COLLISION_FIX_MAIN_WITH_FIELD = _l(
    'Resolve the collision in the main schema. Whatever the change collides with on "%(field)s" exists only'
    ' in main, so it is neither visible nor editable from within the branch; move or delete it, then retry'
    ' the merge. The error above names what it claims.'
)
_REC_COLLISION_FIX_MAIN = _l(
    'Resolve the collision in the main schema. The conflicting object exists only in main, so it is neither'
    ' visible nor editable from within the branch; move or delete it, then retry the merge.'
)
_REC_COLLISION_FIX_BRANCH_WITH_FIELD = _l(
    'Change "%(field)s" on the affected object in the branch to a value that does not collide with the'
    ' main schema, then retry the merge.'
)
_REC_COLLISION_FIX_BRANCH = _l(
    'Change the affected object in the branch so that it no longer collides with the main schema, then'
    ' retry the merge.'
)
_REC_COLLISION_FIX_BRANCH_THEN_SQUASH_WITH_FIELD = _l(
    'Change "%(field)s" on the affected object in the branch, then merge using the Squash strategy. The'
    ' Iterative strategy replays every recorded change in order, so it will apply the original value again'
    ' and fail on the same collision; Squash applies only the final state of each object.'
)
_REC_COLLISION_FIX_BRANCH_THEN_SQUASH = _l(
    'Change the affected object in the branch so that it no longer collides with the main schema, then merge'
    ' using the Squash strategy. The Iterative strategy replays every recorded change in order, so it will'
    ' apply the original value again and fail on the same collision; Squash applies only the final state of'
    ' each object.'
)
_REC_REVIEW_LOG = _l('Review the job log for full error details.')
_REC_TRY_SQUASH_DB = _l(
    'Switch to the Squash merge strategy, which may resolve some database-level conflicts.'
)

__all__ = (
    'annotate_validation_error',
    'build_error_report',
    'get_entry_message',
    'get_merge_recommendations',
)


def annotate_validation_error(exc, model_class, object_id, content_type_id, branch=None):
    """
    Attach branch operation context to a ValidationError before re-raising.

    When ``branch`` is given, the object is additionally re-validated inside its own
    branch schema to work out *where* the offending value is actually invalid. A
    failure that does not reproduce there is not a bad value in the branch at all: it
    is a collision with an object that exists only in main -- two devices independently
    claiming the same rack unit, say -- which nothing the user does to that field inside
    the branch necessarily resolves. Recording that distinction here is what lets the
    job report name the real problem instead of advising the user to "fix" a value that
    is perfectly valid where it lives. (#632)
    """
    exc.netbox_branching_model = model_class
    exc.netbox_branching_object_id = object_id
    exc.netbox_branching_content_type_id = content_type_id
    if branch is not None:
        _flag_main_collision(exc, model_class, object_id, branch)


def _classify_validation_error(exc):
    """
    Return an ``(is_uniqueness, first_field)`` tuple for a ValidationError. ``first_field``
    is None for an error that carries no field mapping at all.
    """
    if hasattr(exc, 'error_dict'):
        for field, field_errors in exc.error_dict.items():
            if any(e.code in ('unique', 'unique_together') for e in field_errors):
                return True, field
        return False, next(iter(exc.error_dict), None)
    if hasattr(exc, 'error_list') and exc.error_list:
        return any(e.code in ('unique', 'unique_together') for e in exc.error_list), None
    return False, None


def _first_error_message(exc, field):
    """Return the underlying validation message for ``field``, for display in the report."""
    if hasattr(exc, 'error_dict'):
        errors = exc.error_dict.get(field) or next(iter(exc.error_dict.values()), None)
    else:
        errors = getattr(exc, 'error_list', None)
    if errors:
        return ' '.join(errors[0].messages)
    return None


def _probe_branch(model_class, object_id, branch, field):
    """
    Re-validate the object as it exists inside its own branch schema.

    Returns a ``(fails_in_branch, value)`` tuple: whether ``field`` is among the fields
    that fail validation *there*, and that field's current value in the branch. Returns
    None if the probe could not be run at all -- the object is gone, the connection is
    unusable, the model does something unexpected during validation. Callers must read
    None as "unknown" and fall back, never as "clean": this runs while an operation is
    already failing, and a probe that cannot answer must not be allowed to invent one.
    """
    logger = logging.getLogger('netbox_branching.error_report')
    try:
        with activate_branch(branch):
            instance = model_class.objects.using(branch.connection_name).get(pk=object_id)
            try:
                full_clean_with_file_check(instance, logger)
            except ValidationError as e:
                failing = set(e.error_dict) if hasattr(e, 'error_dict') else {NON_FIELD_ERRORS}
            else:
                failing = set()
            value = getattr(instance, field, None) if field else None
            return field in failing, str(value) if value is not None else None
    # Deliberately blind: this runs while a merge is already failing, and anything the probe
    # raises must be swallowed rather than replace the user's real ValidationError with an
    # unrelated traceback. Narrowing the list would only trade a missing hint for a crash.
    except Exception as e:  # noqa: BLE001
        logger.debug(f'Branch validity probe failed for {model_class.__name__} {object_id}: {e}')
        return None


def _flag_main_collision(exc, model_class, object_id, branch):
    """
    Mark ``exc`` as a collision with main if the same object validates cleanly inside its
    own branch. Uniqueness errors are left alone: their existing classification already
    points the user at both schemas, so the probe would buy nothing and cost a query.
    """
    is_uniqueness, field = _classify_validation_error(exc)
    if is_uniqueness or object_id is None:
        return
    probe_field = field or NON_FIELD_ERRORS
    if (result := _probe_branch(model_class, object_id, branch, probe_field)) is None:
        return
    fails_in_branch, value = result
    if fails_in_branch:
        return
    exc.netbox_branching_main_collision = True
    exc.netbox_branching_value = value


def _get_field_from_constraint(table_name, constraint_name):
    """
    Return the field name for a single-column unique constraint given its table and constraint name.
    Returns None for composite constraints or if the constraint cannot be found.
    """
    for model in apps.get_models():
        if model._meta.db_table != table_name:
            continue
        for constraint in model._meta.constraints:
            if constraint.name == constraint_name and hasattr(constraint, 'fields'):
                return constraint.fields[0] if len(constraint.fields) == 1 else None
        for field in model._meta.get_fields():
            col = getattr(field, 'column', None)
            auto_names = (f'{table_name}_{col}_key', f'{table_name}_{col}_uniq')
            if col and getattr(field, 'unique', False) and constraint_name in auto_names:
                return field.name
        break
    return None


def _analyze_integrity_error(exc, table_model_map):
    """Parse a Django IntegrityError into a structured report entry (factual data only)."""
    cause = exc.__cause__
    # psycopg3 uses 'sqlstate'; keep 'pgcode' fallback for forward-compatibility.
    sqlstate = getattr(cause, 'sqlstate', None) or getattr(cause, 'pgcode', None)
    diag = getattr(cause, 'diag', None)

    if sqlstate == PG_UNIQUE_VIOLATION:
        # diag attributes are locale-independent catalog values (psycopg3).
        table_name = getattr(diag, 'table_name', None) if diag else None

        # Try constraint_name first (locale-independent) to get the field name.
        constraint_name = getattr(diag, 'constraint_name', None) if diag else None
        field = _get_field_from_constraint(table_name, constraint_name) if constraint_name and table_name else None

        # Parse message_detail for the value (no locale-independent source exists).
        # Also used as fallback for field if constraint lookup didn't resolve it.
        value = None
        if diag and diag.message_detail:
            detail_match = re.search(r'Key \((.+?)\)=\((.+?)\)', diag.message_detail)
            if detail_match:
                if not field:
                    field = detail_match.group(1)
                value = detail_match.group(2)

        return {
            'type': 'unique_constraint',
            'model': table_model_map.get(table_name) if table_name else None,
            'field': field,
            'value': value,
            'detail': None,
            'object_id': None,
            'content_type_id': None,
        }

    return {
        'type': 'database_error',
        'model': None,
        'field': None,
        'value': None,
        'detail': None,
        'object_id': None,
        'content_type_id': None,
    }


def _analyze_validation_error(exc):
    """Parse a Django ValidationError into a structured report entry."""
    model_class = getattr(exc, 'netbox_branching_model', None)
    model_name = model_class._meta.verbose_name if model_class else None

    is_uniqueness, first_field = _classify_validation_error(exc)

    if is_uniqueness:
        error_type = 'unique_constraint'
    elif getattr(exc, 'netbox_branching_main_collision', False):
        error_type = 'main_collision'
    else:
        error_type = 'validation_error'

    return {
        'type': error_type,
        'model': model_name,
        'field': first_field,
        'value': getattr(exc, 'netbox_branching_value', None),
        # The underlying message ("U12 is already occupied...") is the whole point of a
        # collision entry: it names the resource that main has already claimed.
        'detail': _first_error_message(exc, first_field) if error_type == 'main_collision' else None,
        'object_id': getattr(exc, 'netbox_branching_object_id', None),
        'content_type_id': getattr(exc, 'netbox_branching_content_type_id', None),
    }


def build_error_report(exc):
    """
    Analyze an exception and return a structured report entry dict containing:
    type, model, field, value, detail, object_id, content_type_id.
    """
    table_model_map = {model._meta.db_table: model._meta.verbose_name for model in apps.get_models()}
    if isinstance(exc, IntegrityError):
        return _analyze_integrity_error(exc, table_model_map)
    if isinstance(exc, ValidationError):
        return _analyze_validation_error(exc)
    return {
        'type': 'database_error',
        'model': None,
        'field': None,
        'value': None,
        'detail': None,
        'object_id': None,
        'content_type_id': None,
    }


def get_entry_message(entry):
    """Compute a human-readable summary for a report entry."""
    error_type = entry.get('type')
    model = entry.get('model', '')
    field = entry.get('field', '')
    value = entry.get('value', '')

    model_str = model.title() if model else ''
    field_str = f'"{field}"' if field else ''
    value_str = f'"{value}"' if value else ''

    if error_type == 'unique_constraint':
        parts = [p for p in [model_str, field_str, value_str] if p]
        if parts:
            return _('Unique constraint violation: %(base)s already exists in the main schema.') % {
                'base': ' '.join(parts),
            }
        return _('Unique constraint violation: an object already exists in the main schema.')

    if error_type == 'main_collision':
        parts = [p for p in [model_str, field_str] if p]
        where = ' '.join(parts) if parts else _('the affected object')
        if detail := entry.get('detail'):
            return _('Collision with the main schema on %(where)s: %(detail)s') % {
                'where': where,
                'detail': detail,
            }
        return _(
            'Collision with the main schema on %(where)s. The value is valid within the branch; the'
            ' conflict is with an object that exists only in main.'
        ) % {'where': where}

    if error_type == 'validation_error':
        parts = [p for p in [model_str, field_str] if p]
        if parts:
            return _('Validation error on %(where)s.') % {'where': ' '.join(parts)}
        return _('Validation error.')

    return _('An unexpected database error occurred.')


def get_merge_recommendations(entry, merge_strategy=None):
    """Compute actionable recommendations for a failed merge or revert operation."""
    is_squash = merge_strategy == BranchMergeStrategyChoices.SQUASH

    error_type = entry.get('type')
    field = entry.get('field', '')
    value = entry.get('value', '')

    if error_type == 'unique_constraint':
        if field and value:
            rename_rec = _REC_RENAME_WITH_FIELD % {'field': field, 'value': value}
        else:
            rename_rec = _REC_RENAME_GENERIC
        if is_squash:
            return [rename_rec]
        return [rename_rec, _REC_TRY_SQUASH_UNIQUE]

    if error_type == 'main_collision':
        # Changing the value in the branch does not, on its own, let an iterative merge
        # through: iterative replays the recorded changes in order, so it re-applies the
        # original colliding value long before it reaches the change that fixed it. The
        # collision is enforced by a database constraint as well as by clean(), so that
        # intermediate state cannot be materialized in main at all -- only squash, which
        # collapses an object's changes into its final state, avoids it. (#632)
        # Deliberately not interpolating `value` here: it is the branch object's *current*
        # value, which stops matching the contested resource as soon as the user applies the
        # branch-side remedy below and retries (branch device moved to U21, main still holds
        # U12). The message carries the real one.
        if field:
            fix_main = _REC_COLLISION_FIX_MAIN_WITH_FIELD % {'field': field}
            branch_template = (
                _REC_COLLISION_FIX_BRANCH_WITH_FIELD if is_squash else _REC_COLLISION_FIX_BRANCH_THEN_SQUASH_WITH_FIELD
            )
            fix_branch = branch_template % {'field': field}
        else:
            fix_main = _REC_COLLISION_FIX_MAIN
            fix_branch = _REC_COLLISION_FIX_BRANCH if is_squash else _REC_COLLISION_FIX_BRANCH_THEN_SQUASH
        return [fix_main, fix_branch]

    if error_type == 'validation_error':
        if field:
            return [_REC_FIX_FIELD % {'field': field}]
        return [_REC_FIX_GENERIC]

    if is_squash:
        return [_REC_REVIEW_LOG]
    return [_REC_REVIEW_LOG, _REC_TRY_SQUASH_DB]
