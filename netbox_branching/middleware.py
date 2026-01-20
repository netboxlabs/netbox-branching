from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponseBadRequest

from .constants import COOKIE_NAME, QUERY_PARAM
from .utilities import is_api_request, get_active_branch

__all__ = (
    'BranchMiddleware',
)


class BranchMiddleware:
    # Paths that should bypass branch activation
    EXEMPT_PATHS = (
        '/api/status/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):

        response = self.get_response(request)

        # Skip branch activation for exempt paths
        if request.path in self.EXEMPT_PATHS:
            return response

        # Set/clear the active Branch on the request
        try:
            branch = get_active_branch(request)
        except ObjectDoesNotExist:
            return HttpResponseBadRequest("Invalid branch identifier")

        # Set/clear the branch cookie (for non-API requests)
        if not is_api_request(request):
            if branch:
                response.set_cookie(COOKIE_NAME, branch.schema_id)
            elif QUERY_PARAM in request.GET:
                response.delete_cookie(COOKIE_NAME)

        return response
