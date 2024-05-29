from .contextvars import active_context


__all__ = (
    'ContextAwareRouter',
)


class ContextAwareRouter:
    """
    A Django database router that returns the appropriate connection/schema for
    the active context (if any).
    """
    def db_for_read(self, model, **hints):
        if context := active_context.get():
            return f'schema_{context.schema_name}'
        return None

    def db_for_write(self, model, **hints):
        if context := active_context.get():
            return f'schema_{context.schema_name}'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        # Permit relations from the context schema to the primary (public) schema
        return True
