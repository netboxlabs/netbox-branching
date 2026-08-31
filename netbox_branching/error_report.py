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
    'Fix the invalid value for field "%(field)s" on the affected object in the branch before retrying.'
)
_REC_FIX_GENERIC = _l(
    'Fix the invalid value on the affected object in the branch before retrying.'
)
_REC_SYNC_AND_RESOLVE_WITH_FIELD = _l(
    'Sync the branch to pull in the conflicting data from main, then change %(field)s on the'
    ' affected object within the branch so that it no longer conflicts, and merge again.'
)
_REC_SYNC_AND_RESOLVE_GENERIC = _l(
    'Sync the branch to pull in the conflicting data from main, then resolve the conflict on the'
    ' affected object within the branch and merge again.'
)
_REC_CHANGE_MAIN = _l(
    'Change the conflicting object in the main schema so that the branch\'s change can be applied.'
)
_REC_TRY_SQUASH_VALIDATION = _l(
    'Switch to the Squash merge strategy, which applies only each object\'s final state instead of'
    ' replaying every intermediate change. This is required when the conflicting value was corrected'
    ' within the branch after it was first recorded.'
)
_REC_REVIEW_LOG = _l('Review the job log for full error details.')
_REC_TRY_SQUASH_DB = _l(
    'Switch to the Squash merge strategy, which may resolve some database-level conflicts.'
)

__all__ = (
    'annotate_validation_error',
    'build_error_report',
    'describe_validation_failure',
    'get_entry_message',
    'get_merge_recommendations',
)


def describe_validation_failure(exc):
    """
    Render a ValidationError or IntegrityError as a single human-readable string, keeping
    field names where the exception carries them.
    """
    if isinstance(exc, ValidationError):
        if hasattr(exc, 'error_dict'):
            return '; '.join(
                ' '.join(str(m) for m in messages) if field == NON_FIELD_ERRORS
                else f'{field}: {" ".join(str(m) for m in messages)}'
                for field, messages in exc.message_dict.items()
            )
        return ' '.join(str(m) for m in exc.messages)
    return ' '.join(str(exc).split())


def annotate_validation_error(exc, model_class, object_id, content_type_id, branch=None):
    """Attach branch operation context to a ValidationError before re-raising."""
    exc.netbox_branching_model = model_class
    exc.netbox_branching_object_id = object_id
    exc.netbox_branching_content_type_id = content_type_id
    if branch is not None:
        exc.netbox_branching_valid_in_branch = _validates_in_branch(branch, model_class, object_id)


def _validates_in_branch(branch, model_class, object_id):
    """
    Return True if the object passes model validation inside the branch schema, False if it
    does not, or None if the check could not be performed.

    A replayed change which main rejects but the branch accepts is colliding with data
    outside the branch's own change set. (#632)
    """
    from .utilities import activate_branch

    try:
        instance = model_class.objects.using(branch.connection_name).get(pk=object_id)
        with activate_branch(branch):
            instance.full_clean()
    except ValidationError:
        return False
    except Exception:  # noqa: BLE001 — never mask the error being reported
        return None
    return True


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

    is_uniqueness = False
    first_field = None

    if hasattr(exc, 'error_dict'):
        for field, field_errors in exc.error_dict.items():
            if any(e.code in ('unique', 'unique_together') for e in field_errors):
                is_uniqueness = True
                first_field = field
                break
        if not is_uniqueness:
            first_field = next(iter(exc.error_dict), None)
    elif hasattr(exc, 'error_list') and exc.error_list:
        is_uniqueness = any(e.code in ('unique', 'unique_together') for e in exc.error_list)

    if is_uniqueness:
        error_type = 'unique_constraint'
    elif getattr(exc, 'netbox_branching_valid_in_branch', None):
        # The object validates within the branch and was rejected only by main, so the
        # cause lies outside the branch's own change set. (#632)
        error_type = 'validation_conflict'
    else:
        error_type = 'validation_error'

    return {
        'type': error_type,
        'model': model_name,
        'field': first_field,
        'value': None,
        'detail': ' '.join(str(m) for m in exc.messages) or None,
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
    detail = entry.get('detail', '')

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

    if error_type == 'validation_conflict':
        parts = [p for p in [model_str, field_str] if p]
        if parts:
            base = _('%(where)s conflicts with data in the main schema.') % {'where': ' '.join(parts)}
        else:
            base = _('The change conflicts with data in the main schema.')
        return f'{base} {detail}' if detail else base

    if error_type == 'validation_error':
        parts = [p for p in [model_str, field_str] if p]
        if parts:
            base = _('Validation error on %(where)s.') % {'where': ' '.join(parts)}
        else:
            base = _('Validation error.')
        return f'{base} {detail}' if detail else base

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

    if error_type == 'validation_conflict':
        if field:
            recs = [_REC_SYNC_AND_RESOLVE_WITH_FIELD % {'field': f'"{field}"'}]
        else:
            recs = [_REC_SYNC_AND_RESOLVE_GENERIC]
        if not is_squash:
            # Iterative replays every intermediate state, so a corrected branch object still
            # collides; squash applies only the final state.
            recs.append(_REC_TRY_SQUASH_VALIDATION)
        recs.append(_REC_CHANGE_MAIN)
        return recs

    if error_type == 'validation_error':
        if field:
            return [_REC_FIX_FIELD % {'field': field}]
        return [_REC_FIX_GENERIC]

    if is_squash:
        return [_REC_REVIEW_LOG]
    return [_REC_REVIEW_LOG, _REC_TRY_SQUASH_DB]
