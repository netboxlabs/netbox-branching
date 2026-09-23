import re

from django.apps import apps
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import IntegrityError
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from .choices import BranchMergeStrategyChoices
from .constants import PG_UNIQUE_VIOLATION

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
    'Resolve the condition described above. If the value of "%(field)s" is valid within the branch, it'
    ' collides with an object that exists only in main — that object is neither visible nor editable from'
    ' within the branch, so move or delete it there.'
)
_REC_FIX_GENERIC = _l(
    'Resolve the condition described above. If the affected object is valid within the branch, it collides'
    ' with an object that exists only in main — that object is neither visible nor editable from within the'
    ' branch, so move or delete it there.'
)
_REC_FIX_IN_BRANCH_THEN_SQUASH = _l(
    'If you resolve it by changing the object in the branch, merge using the Squash merge strategy. The'
    ' Iterative strategy replays every recorded change in order, so it will apply the original value again'
    ' and fail identically; Squash applies only the final state of each object.'
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


def annotate_validation_error(exc, model_class, object_id, content_type_id):
    """Attach branch operation context to a ValidationError before re-raising."""
    exc.netbox_branching_model = model_class
    exc.netbox_branching_object_id = object_id
    exc.netbox_branching_content_type_id = content_type_id


def _classify_validation_error(exc):
    """
    Return an ``(is_uniqueness, first_field)`` tuple for a ValidationError. ``first_field`` is
    None for an error that names no field -- including one keyed on Django's NON_FIELD_ERRORS
    sentinel, which must never reach the report as if it were a field.
    """
    def named(field):
        return None if field == NON_FIELD_ERRORS else field

    if hasattr(exc, 'error_dict'):
        for field, field_errors in exc.error_dict.items():
            if any(e.code in ('unique', 'unique_together') for e in field_errors):
                return True, named(field)
        return False, named(next(iter(exc.error_dict), None))
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

    return {
        'type': 'unique_constraint' if is_uniqueness else 'validation_error',
        'model': model_name,
        'field': first_field,
        'value': None,
        # The underlying message, which often names the state in main that blocked the change (#632)
        'detail': _first_error_message(exc, first_field),
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

    if error_type == 'validation_error':
        parts = [p for p in [model_str, field_str] if p]
        where = ' '.join(parts) if parts else _('the affected object')
        if detail := entry.get('detail'):
            return _('Validation error on %(where)s: %(detail)s') % {'where': where, 'detail': detail}
        return _('Validation error on %(where)s.') % {'where': where}

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

    if error_type == 'validation_error':
        # Iterative replays the original recorded value, so a branch-side fix only takes effect
        # under squash -- true of any replayed change, not just a collision with main. (#632)
        recs = [_REC_FIX_FIELD % {'field': field} if field else _REC_FIX_GENERIC]
        if not is_squash:
            recs.append(_REC_FIX_IN_BRANCH_THEN_SQUASH)
        return recs

    if is_squash:
        return [_REC_REVIEW_LOG]
    return [_REC_REVIEW_LOG, _REC_TRY_SQUASH_DB]
