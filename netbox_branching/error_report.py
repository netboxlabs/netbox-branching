import re

from django.apps import apps
from django.core.exceptions import ValidationError
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
_REC_REVIEW_LOG = _l('Review the job log for full error details.')
_REC_TRY_SQUASH_DB = _l(
    'Switch to the Squash merge strategy, which may resolve some database-level conflicts.'
)

__all__ = (
    'build_error_report',
    'get_entry_message',
    'get_merge_recommendations',
)


def annotate_validation_error(exc, model_class, object_id, content_type_id):
    """Attach branch operation context to a ValidationError before re-raising."""
    exc.netbox_branching_model = model_class
    exc.netbox_branching_object_id = object_id
    exc.netbox_branching_content_type_id = content_type_id


def _analyze_integrity_error(exc, table_model_map):
    """Parse a Django IntegrityError into a structured report entry (factual data only)."""
    cause = exc.__cause__
    # psycopg3 uses 'sqlstate'; keep 'pgcode' fallback for forward-compatibility.
    sqlstate = getattr(cause, 'sqlstate', None) or getattr(cause, 'pgcode', None)
    diag = getattr(cause, 'diag', None)

    if sqlstate == PG_UNIQUE_VIOLATION:
        # diag attributes are locale-independent catalog values (psycopg3).
        table_name = getattr(diag, 'table_name', None) if diag else None

        # Format: "Key (field)=(value) already exists." — locale-dependent; returns None
        # for non-English PostgreSQL locales, falling back to the generic message.
        field = None
        value = None
        if diag and diag.message_detail:
            detail_match = re.search(r'Key \((.+?)\)=\((.+?)\)', diag.message_detail)
            if detail_match:
                field = detail_match.group(1)
                value = detail_match.group(2)

        return {
            'type': 'unique_constraint',
            'model': table_model_map.get(table_name) if table_name else None,
            'field': field,
            'value': value,
            'object_id': None,
            'content_type_id': None,
        }

    return {
        'type': 'database_error',
        'model': None,
        'field': None,
        'value': None,
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

    return {
        'type': 'unique_constraint' if is_uniqueness else 'validation_error',
        'model': model_name,
        'field': first_field,
        'value': None,
        'object_id': getattr(exc, 'netbox_branching_object_id', None),
        'content_type_id': getattr(exc, 'netbox_branching_content_type_id', None),
    }


def build_error_report(exc):
    """
    Analyze an exception and return a structured report entry dict containing:
    type, model, field, value, object_id, content_type_id.
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

    if error_type == 'validation_error':
        if field:
            return [_REC_FIX_FIELD % {'field': field}]
        return [_REC_FIX_GENERIC]

    if is_squash:
        return [_REC_REVIEW_LOG]
    return [_REC_REVIEW_LOG, _REC_TRY_SQUASH_DB]
