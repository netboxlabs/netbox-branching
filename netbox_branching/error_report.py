import re

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils.translation import gettext as _

__all__ = ('build_error_report', 'get_entry_message', 'get_merge_recommendations', 'get_sync_recommendations')

# PostgreSQL error codes
PG_UNIQUE_VIOLATION = '23505'
PG_FK_VIOLATION = '23503'
PG_NOT_NULL_VIOLATION = '23502'
PG_CHECK_VIOLATION = '23514'


def _table_to_model(table_name):
    """Return the verbose_name for a Django model given its DB table name, or None."""
    for model in apps.get_models():
        if model._meta.db_table == table_name:
            return model._meta.verbose_name
    return None


def _analyze_integrity_error(exc):
    """Parse a Django IntegrityError into a structured report entry (factual data only)."""
    cause = exc.__cause__
    pgcode = getattr(cause, 'pgcode', None)
    pgerror = getattr(cause, 'pgerror', '') or ''

    if pgcode == PG_UNIQUE_VIOLATION:
        detail_match = re.search(r'DETAIL:\s+Key \((.+?)\)=\((.+?)\) already exists', pgerror)
        table_match = re.search(r'relation "([^"]+)"', pgerror)
        return {
            'type': 'unique_constraint',
            'model': _table_to_model(table_match.group(1)) if table_match else None,
            'field': detail_match.group(1) if detail_match else None,
            'value': detail_match.group(2) if detail_match else None,
        }

    if pgcode == PG_FK_VIOLATION:
        return {
            'type': 'fk_violation',
            'model': None,
            'field': None,
            'value': None,
        }

    if pgcode == PG_NOT_NULL_VIOLATION:
        col_match = re.search(r'column "([^"]+)"', pgerror)
        return {
            'type': 'not_null_violation',
            'model': None,
            'field': col_match.group(1) if col_match else None,
            'value': None,
        }

    if pgcode == PG_CHECK_VIOLATION:
        return {
            'type': 'check_violation',
            'model': None,
            'field': None,
            'value': None,
        }

    return {
        'type': 'database_error',
        'model': None,
        'field': None,
        'value': None,
    }


def _analyze_validation_error(exc):
    """Parse a Django ValidationError into a structured report entry."""
    model_class = getattr(exc, 'netbox_branching_model', None)
    model_name = model_class._meta.verbose_name if model_class else None

    is_uniqueness = False
    first_field = None

    if hasattr(exc, 'message_dict'):
        first_field = next(iter(exc.message_dict), None)
        for errors in exc.message_dict.values():
            if any('already exists' in e for e in errors):
                is_uniqueness = True
                break
    elif hasattr(exc, 'messages') and exc.messages:
        is_uniqueness = any('already exists' in m for m in exc.messages)

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
    if isinstance(exc, IntegrityError):
        return _analyze_integrity_error(exc)
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

    if error_type == 'fk_violation':
        return _('Foreign key violation: a referenced object does not exist in the main schema.')

    if error_type == 'not_null_violation':
        if field_str:
            return _('Not-null constraint violation on field %(field)s: a required field has no value.') % {
                'field': field_str,
            }
        return _('Not-null constraint violation: a required field has no value.')

    if error_type == 'check_violation':
        return _('Check constraint violation: a field value did not satisfy a database constraint.')

    if error_type == 'validation_error':
        parts = [p for p in [model_str, field_str] if p]
        if parts:
            return _('Validation error on %(where)s.') % {'where': ' '.join(parts)}
        return _('Validation error.')

    return _('An unexpected database error occurred.')


def get_sync_recommendations(entry):
    """Compute actionable recommendations for a failed sync operation."""
    error_type = entry.get('type')
    field = entry.get('field', '')
    value = entry.get('value', '')

    if error_type == 'unique_constraint':
        if field and value:
            return [
                _('Rename the conflicting object (where %(field)s="%(value)s") in either the branch or the main schema,'
                  ' then retry the sync.') % {
                    'field': field,
                    'value': value,
                },
            ]
        return [
            _('Rename the conflicting object in either the branch or the main schema so the values no longer conflict,'
              ' then retry the sync.'),
        ]

    if error_type == 'fk_violation':
        return [
            _('Ensure all objects referenced by the incoming changes exist in the branch schema, then retry the sync.'),
        ]

    if error_type in ('not_null_violation', 'check_violation', 'validation_error'):
        if field:
            return [
                _('Fix the invalid value for field "%(field)s" on the affected object in the branch,'
                  ' then retry the sync.') % {
                    'field': field,
                },
            ]
        return [
            _('Fix the invalid value on the affected object in the branch, then retry the sync.'),
        ]

    return [_('Review the job log for full error details, then retry the sync.')]


def get_merge_recommendations(entry):
    """Compute actionable recommendations for a failed merge or revert operation."""
    error_type = entry.get('type')
    field = entry.get('field', '')
    value = entry.get('value', '')

    if error_type == 'unique_constraint':
        if field and value:
            rename_rec = _('Rename the conflicting object (where %(field)s="%(value)s") in either the branch'
                           ' or the main schema.') % {
                'field': field,
                'value': value,
            }
        else:
            rename_rec = _('Rename the conflicting object in either the branch or the main schema'
                           ' so the values no longer conflict.')
        return [
            rename_rec,
            _('Switch to the Squash merge strategy, which handles unique constraint conflicts automatically.'),
        ]

    if error_type == 'fk_violation':
        return [
            _('Ensure all objects referenced by the branch changes exist in the main schema.'),
            _('Switch to the Squash merge strategy, which resolves dependency ordering automatically.'),
        ]

    if error_type in ('not_null_violation', 'check_violation', 'validation_error'):
        if field:
            return [
                _('Fix the invalid value for field "%(field)s" on the affected object in the branch'
                  ' before retrying.') % {
                    'field': field,
                },
            ]
        return [
            _('Fix the invalid value on the affected object in the branch before retrying.'),
        ]

    return [
        _('Review the job log for full error details.'),
        _('Switch to the Squash merge strategy, which may resolve some database-level conflicts.'),
    ]
