from .utilities import get_active_context


__all__ = (
    'ContextAwareRouter',
)


class ContextAwareRouter:
    """
    A Django database router that returns the appropriate connection/schema for
    the active context (if any).
    """
    def db_for_read(self, model, **hints):
        if active_context := get_active_context():
            return f'schema_{active_context}'
        return None

    def db_for_write(self, model, **hints):
        if active_context := get_active_context():
            return f'schema_{active_context}'
        return None
