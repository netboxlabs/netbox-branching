from django.contrib.sessions.models import Session

from netbox.context import current_request

from .models import Context


__all__ = (
    'ContextAwareRouter',
)


class ContextAwareRouter:
    """
    A Django database router that returns the appropriate connection/schema for
    the active context (if any).
    """
    def _get_active_schema(self):
        if request := current_request.get():
            if active_context := request.session.get('context'):
                context = Context.objects.using('default').get(pk=active_context)
                # TODO: Store a slug on Context model for schema name
                return context.name.lower()

    def db_for_read(self, model, **hints):
        if model is Session:
            return None
        if schema := self._get_active_schema():
            return f'schema_{schema}'
        return None

    def db_for_write(self, model, **hints):
        if model is Session:
            return None
        if schema := self._get_active_schema():
            return f'schema_{schema}'
        return None
