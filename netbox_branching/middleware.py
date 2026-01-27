from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponseBadRequest, HttpResponseRedirect

from .constants import COOKIE_NAME, EXEMPT_PATHS, QUERY_PARAM
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

        # Skip branch activation for exempt paths
        if request.path in EXEMPT_PATHS:
            return self.get_response(request)

        # Set/clear the active Branch on the request
        try:
            branch = get_active_branch(request)
            # Track if the user explicitly activated/deactivated a branch via query parameter
            branch_change = QUERY_PARAM in request.GET
        except ObjectDoesNotExist:
            return HttpResponseBadRequest("Invalid branch identifier")

        response = self.get_response(request)

        # Set/clear the branch cookie (for non-API requests)
        if not is_api_request(request):
            if branch:
                response.set_cookie(COOKIE_NAME, branch.schema_id)
            elif branch_change:
                response.delete_cookie(COOKIE_NAME)

            # Redirect to dashboard if branch activation/deactivation results in 404
            if branch_change and response.status_code == 404:
                messages.warning(request, "The requested object does not exist in the current branch.")
                return HttpResponseRedirect('/')

        return response
