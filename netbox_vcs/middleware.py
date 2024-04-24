from .models import Context

__all__ = (
    'ContextMiddleware',
)


class ContextMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):

        # Update the active Context if specified
        if context_id := request.GET.get('_context'):
            if context := Context.objects.get(pk=context_id):
                request.session['context'] = context.pk
        elif '_context' in request.GET:
            request.session['context'] = None

        return self.get_response(request)
