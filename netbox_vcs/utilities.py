from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property

from extras.choices import ObjectChangeActionChoices
from .contextvars import active_context

__all__ = (
    'ChangeDiff',
    'DynamicSchemaDict',
    'activate_context',
    'deactivate_context',
    'get_context_aware_object_types',
    'get_tables_to_replicate',
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


@dataclass
class ChangeDiff:
    object: object = None
    object_repr: str = ''
    action: str = ''
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    current: dict = field(default_factory=dict)

    @cached_property
    def conflicts(self):
        if self.action == ObjectChangeActionChoices.ACTION_CREATE:
            # Newly created objects cannot have change conflicts
            return []
        return [
            k for k, v in self.current.items()
            if self.before[k] != self.current[k]
        ]


@contextmanager
def activate_context(context):
    """
    A context manager for activating a Context.
    """
    token = active_context.set(context)

    yield

    active_context.reset(token)


@contextmanager
def deactivate_context():
    """
    A context manager for temporarily deactivating the active Context (if any). This is a
    convenience function for `activate_context(None)`.
    """
    token = active_context.set(None)

    yield

    active_context.reset(token)


def get_context_aware_object_types():
    """
    Return all object types which are context aware; i.e. those which support change logging.
    """
    from core.models import ObjectType
    return ObjectType.objects.with_feature('change_logging').exclude(app_label='netbox_vcs')


def get_tables_to_replicate():
    """
    Returned an ordered list of database tables to replicate when provisioning a new schema.
    """
    tables = set()

    context_aware_models = [
        ot.model_class() for ot in get_context_aware_object_types()
    ]
    for model in context_aware_models:

        # Capture the model's table
        tables.add(model._meta.db_table)

        # Capture any M2M fields which reference other replicated models
        for m2m_field in model._meta.local_many_to_many:
            if m2m_field.related_model in context_aware_models:
                if hasattr(m2m_field, 'through'):
                    # Field is actually a manager
                    m2m_table = m2m_field.through._meta.db_table
                else:
                    m2m_table = m2m_field._get_m2m_db_table(model._meta)
                tables.add(m2m_table)

    return sorted(tables)
