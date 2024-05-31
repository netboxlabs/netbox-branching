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
