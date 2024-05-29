from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponseBadRequest

from utilities.api import is_api_request

from .constants import COOKIE_NAME, CONTEXT_HEADER, QUERY_PARAM
from .models import Context

__all__ = (
    'ContextMiddleware',
)


class ContextMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):

        # Set/clear the active Context on the request
        try:
            self.set_active_context(request)
        except ObjectDoesNotExist:
            return HttpResponseBadRequest("Invalid context identifier")

        response = self.get_response(request)

        # Set/clear the context cookie (for non-API requests)
        if not is_api_request(request):
            if request.context:
                response.set_cookie('active_context', request.context.schema_id)
            elif '_context' in request.GET:
                response.delete_cookie('active_context')

        return response

    @staticmethod
    def set_active_context(request):
        """
        Set the active Context (if any) on the request object.
        """
        request.context = None

        # The active Context is specified by HTTP header for REST API requests.
        if is_api_request(request) and (schema_id := request.headers.get(CONTEXT_HEADER)):
            request.context = Context.objects.get(schema_id=schema_id)

        # Context activated/deactivated by URL query parameter
        elif QUERY_PARAM in request.GET:
            if schema_id := request.GET.get(QUERY_PARAM):
                request.context = Context.objects.get(schema_id=schema_id)
            else:
                request.COOKIES.pop(COOKIE_NAME, None)  # Delete cookie

        # Context set by cookie
        elif schema_id := request.COOKIES.get('active_context'):
            request.context = Context.objects.filter(schema_id=schema_id).first()
