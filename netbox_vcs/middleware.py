from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponseBadRequest

from .models import Context

__all__ = (
    'ContextMiddleware',
)


class ContextMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):

        # Activating a context
        if schema_id := request.GET.get('_context'):
            try:
                context = Context.objects.get(schema_id=schema_id)
                request.context = context
                request.COOKIES['active_context'] = context.schema_id
            except ObjectDoesNotExist:
                return HttpResponseBadRequest(f"Context {schema_id} not found")

        # Deactivating the current schema
        elif '_context' in request.GET:
            del request.COOKIES['active_context']
            request.context = None

        # Infer the active context from cookie
        elif schema_id := request.COOKIES.get('active_context'):
            request.context = Context.objects.get(schema_id=schema_id)
        else:
            request.context = None

        response = self.get_response(request)

        if request.context:
            response.set_cookie('active_context', request.context.schema_id)
        elif '_context' in request.GET:
            response.delete_cookie('active_context')

        return response
