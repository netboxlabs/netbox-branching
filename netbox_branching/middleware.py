from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponseBadRequest, HttpResponseRedirect

from .constants import COOKIE_NAME, EXEMPT_PATHS, QUERY_PARAM
from .utilities import get_active_branch, is_api_request

__all__ = (
    'BranchMiddleware',
)


class BranchMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def _apply_branch_cookie(self, response, branch, branch_change):
        """
        Apply or remove the branch cookie on the given response.

        Cookie attributes mirror Django's SESSION_COOKIE_* settings so the
        branch cookie is treated consistently with the session cookie by the
        browser (in particular, SameSite must be set explicitly — browsers
        that don't default to Lax will otherwise drop the cookie on
        cross-site navigation, causing branch writes to leak to main).
        """
        if branch:
            response.set_cookie(
                COOKIE_NAME,
                branch.schema_id,
                domain=settings.SESSION_COOKIE_DOMAIN,
                path=settings.SESSION_COOKIE_PATH,
                secure=settings.SESSION_COOKIE_SECURE,
                samesite=settings.SESSION_COOKIE_SAMESITE,
            )
        elif branch_change:
            response.delete_cookie(
                COOKIE_NAME,
                domain=settings.SESSION_COOKIE_DOMAIN,
                path=settings.SESSION_COOKIE_PATH,
                samesite=settings.SESSION_COOKIE_SAMESITE,
            )

    def __call__(self, request):

        # Skip branch activation for exempt paths
        if request.path in EXEMPT_PATHS:
            return self.get_response(request)

        # Set/clear the active Branch on the request
        try:
            branch = get_active_branch(request)
        except ObjectDoesNotExist:
            return HttpResponseBadRequest("Invalid branch identifier")
        request.active_branch = branch

        response = self.get_response(request)

        # Set/clear the branch cookie (for non-API requests)
        if not is_api_request(request):
            # Check if a branch is being activated/deactivated, or if a stale
            # cookie exists (cookie present but branch not active — e.g. branch
            # was archived or merged since the cookie was set).
            branch_change = QUERY_PARAM in request.GET or (COOKIE_NAME in request.COOKIES and not branch)

            # Redirect to dashboard if branch activation/deactivation results in 404
            if branch_change and response.status_code == 404:
                # Construct a more informative error message
                branch_name = f"branch '{branch.name}'" if branch else "main"
                requested_url = request.path
                messages.warning(
                    request,
                    f"The requested object at {requested_url} does not exist in {branch_name}."
                )

                # Create redirect response and apply cookie operations to it
                redirect_response = HttpResponseRedirect('/')
                self._apply_branch_cookie(redirect_response, branch, branch_change)
                return redirect_response

            # Set/clear cookie on the normal response
            self._apply_branch_cookie(response, branch, branch_change)

        return response
