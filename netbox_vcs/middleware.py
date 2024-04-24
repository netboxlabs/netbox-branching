from .models import Context

__all__ = (
    'ContextMiddleware',
)


class ContextMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        active_context = None

        # Update the active Context if specified
        if context := request.GET.get('_context'):
            if Context.objects.filter(schema_name=context).exists():
                active_context = context
                request.COOKIES['active_context'] = active_context
        elif '_context' in request.GET:
            del request.COOKIES['active_context']
            active_context = None

        response = self.get_response(request)

        if active_context:
            response.set_cookie('active_context', active_context)
        elif '_context' in request.GET:
            response.delete_cookie('active_context')

        return response
