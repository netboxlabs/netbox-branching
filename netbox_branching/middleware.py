from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponseBadRequest
from django.urls import reverse

from utilities.api import is_api_request

from .choices import BranchStatusChoices
from .constants import COOKIE_NAME, BRANCH_HEADER, QUERY_PARAM
from .models import Branch
from .utilities import activate_branch, is_api_request

__all__ = (
    'BranchMiddleware',
)


class BranchMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):

        # Set/clear the active Branch on the request
        try:
            branch = self.get_active_branch(request)
        except ObjectDoesNotExist:
            return HttpResponseBadRequest("Invalid branch identifier")

        with activate_branch(branch):
            response = self.get_response(request)

        # Set/clear the branch cookie (for non-API requests)
        if not is_api_request(request):
            if branch:
                response.set_cookie(COOKIE_NAME, branch.schema_id)
            elif QUERY_PARAM in request.GET:
                response.delete_cookie(COOKIE_NAME)

        return response

    @staticmethod
    def get_active_branch(request):
        """
        Return the active Branch (if any).
        """
        # The active Branch may be specified by HTTP header for REST & GraphQL API requests.
        if is_api_request(request) and BRANCH_HEADER in request.headers:
            branch = Branch.objects.get(schema_id=request.headers.get(BRANCH_HEADER))
            if not branch.ready:
                return HttpResponseBadRequest(f"Branch {branch} is not ready for use (status: {branch.status})")
            return branch

        # Branch activated/deactivated by URL query parameter
        elif QUERY_PARAM in request.GET:
            if schema_id := request.GET.get(QUERY_PARAM):
                branch = Branch.objects.get(schema_id=schema_id)
                if branch.ready:
                    messages.success(request, f"Activated branch {branch}")
                    return branch
                else:
                    messages.error(request, f"Branch {branch} is not ready for use (status: {branch.status})")
                    return None
            else:
                messages.success(request, f"Deactivated branch")
                request.COOKIES.pop(COOKIE_NAME, None)  # Delete cookie if set
                return None

        # Branch set by cookie
        elif schema_id := request.COOKIES.get(COOKIE_NAME):
            return Branch.objects.filter(schema_id=schema_id, status=BranchStatusChoices.READY).first()
