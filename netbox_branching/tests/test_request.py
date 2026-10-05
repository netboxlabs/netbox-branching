import warnings
from unittest.mock import patch

from django.test import RequestFactory, override_settings
from django.urls import reverse
from utilities.request import apply_request_processors
from utilities.testing import TestCase

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import COOKIE_NAME, QUERY_PARAM
from netbox_branching.contextvars import active_branch
from netbox_branching.models import Branch
from netbox_branching.utilities import ActiveBranchContextManager


class RequestTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        # Create a Branch
        branch = Branch(name='Branch 1')
        branch.status = BranchStatusChoices.READY  # Fake provisioning
        branch.save(provision=False)

    @override_settings(
        LOGIN_REQUIRED=False,
        SESSION_COOKIE_DOMAIN='example.com',
        SESSION_COOKIE_PATH='/custom',
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE='Strict',
    )
    def test_activate_branch(self):
        branch = Branch.objects.first()

        # Activate the Branch
        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}={branch.schema_id}')
        self.assertEqual(response.status_code, 200)
        self.assertIn(COOKIE_NAME, self.client.cookies, msg="Cookie was not set on response")
        self.assertEqual(
            self.client.cookies[COOKIE_NAME].value,
            branch.schema_id,
            msg="Branch ID set in cookie is incorrect"
        )

        # Cookie attributes should mirror SESSION_COOKIE_* settings
        cookie = response.cookies[COOKIE_NAME]
        self.assertEqual(cookie['domain'], 'example.com')
        self.assertEqual(cookie['path'], '/custom')
        self.assertTrue(cookie['secure'])
        self.assertEqual(cookie['samesite'], 'Strict')

        # Verify exactly one activation toast (not duplicated by the request processor)
        messages_list = list(response.wsgi_request._messages)
        self.assertEqual(len(messages_list), 1, msg="Expected exactly one activation toast message")

    @override_settings(
        LOGIN_REQUIRED=False,
        SESSION_COOKIE_DOMAIN='example.com',
        SESSION_COOKIE_PATH='/custom',
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE='Strict',
    )
    def test_deactivate_branch(self):
        # Attach the cookie to the test client
        branch = Branch.objects.first()
        self.client.cookies.load({
            COOKIE_NAME: branch.schema_id,
        })

        # Deactivate the Branch
        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}=')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, '', msg="Cookie was not deleted")

        # Deletion cookie attributes should mirror SESSION_COOKIE_* settings
        cookie = response.cookies[COOKIE_NAME]
        self.assertEqual(cookie['domain'], 'example.com')
        self.assertEqual(cookie['path'], '/custom')
        self.assertEqual(cookie['samesite'], 'Strict')

    @override_settings(LOGIN_REQUIRED=False)
    def test_reactivate_branch_no_message(self):
        branch = Branch.objects.first()
        self.client.cookies.load({
            COOKIE_NAME: branch.schema_id,
        })

        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}={branch.schema_id}')
        self.assertEqual(response.status_code, 200)
        messages_list = list(response.wsgi_request._messages)
        self.assertEqual(len(messages_list), 0, msg="Unexpected toast message on branch re-activation")

    @override_settings(LOGIN_REQUIRED=False)
    def test_stale_cookie_cleared(self):
        """
        A cookie referencing a non-ready branch should be automatically cleared.
        """
        branch = Branch.objects.first()
        branch.status = BranchStatusChoices.ARCHIVED
        branch.save(provision=False, update_merge_sync_fields=True)

        self.client.cookies.load({
            COOKIE_NAME: branch.schema_id,
        })

        url = reverse('home')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, '', msg="Stale cookie was not cleared")

    # -------------------------------------------------------------------------
    # Paranoid paths
    #
    # The middleware catches ObjectDoesNotExist from get_active_branch() and
    # returns HTTP 400. These tests pin that contract down so a refactor that
    # narrows the except clause (or stops catching at all) is caught early —
    # a non-existent branch ID slipping through would otherwise produce a 500
    # somewhere downstream where the failure mode is harder to interpret.
    # -------------------------------------------------------------------------

    @override_settings(LOGIN_REQUIRED=False)
    def test_query_param_with_nonexistent_branch_returns_400(self):
        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}=nonexist')
        self.assertEqual(response.status_code, 400)

    @override_settings(LOGIN_REQUIRED=False)
    def test_api_header_with_nonexistent_branch_returns_400(self):
        """
        get_active_branch routes API requests with the X-NetBox-Branch header
        through Branch.objects.get(), which raises Branch.DoesNotExist for an
        unknown schema_id — caught by the middleware and surfaced as 400.
        """
        response = self.client.get(
            reverse('api-root'),
            HTTP_X_NETBOX_BRANCH='nonexist',
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(LOGIN_REQUIRED=False)
    def test_api_header_with_unready_branch_returns_400(self):
        """
        A branch which exists but is not ready must be refused, not activated. get_active_branch()
        used to return an HttpResponseBadRequest here, and both of its callers treat the return
        value as a Branch: ActiveBranchContextManager installed the response object as the active
        branch and BranchMiddleware assigned it to request.active_branch, so the first branchable
        query raised AttributeError and the intended 400 surfaced as a 500 instead. See #672.
        """
        self.add_permissions('dcim.view_site')
        branch = Branch.objects.first()
        Branch.objects.filter(pk=branch.pk).update(status=BranchStatusChoices.SYNCING)

        response = self.client.get(
            reverse('dcim-api:site-list'),
            HTTP_X_NETBOX_BRANCH=branch.schema_id,
        )

        self.assertEqual(response.status_code, 400)
        body = response.content.decode()
        self.assertIn('Selected branch is not ready', body)

        # The refusal is a static message: the BranchNotReady text names the branch and its status,
        # and must not be reflected back to the client (CodeQL: information exposure through an
        # exception).
        self.assertNotIn(branch.name, body, msg="Branch name leaked into the 400 response")
        self.assertNotIn(BranchStatusChoices.SYNCING, body, msg="Branch status leaked into the 400 response")

    def test_unready_branch_is_never_activated(self):
        """
        The request processor runs ahead of BranchMiddleware (plugin middleware is appended after
        CoreMiddleware, which applies the request processors), so it cannot rely on the middleware
        to keep an unusable branch out of the context var — it has to refuse on its own. See #672.
        """
        branch = Branch.objects.first()
        Branch.objects.filter(pk=branch.pk).update(status=BranchStatusChoices.SYNCING)

        request = RequestFactory().get(
            reverse('dcim-api:site-list'),
            headers={'x-netbox-branch': branch.schema_id},
        )

        with ActiveBranchContextManager(request):
            self.assertIsNone(active_branch.get(), msg="An unusable branch was installed as active")

    def test_nonexistent_branch_is_refused_without_warning(self):
        """
        A request naming a branch which does not exist is refused by BranchMiddleware, but the
        request processor runs first and must not let Branch.DoesNotExist escape: it would be
        swallowed by apply_request_processors(), which reports the expected refusal as a failed
        request processor on every such request. See #672.
        """
        request = RequestFactory().get(
            reverse('dcim-api:site-list'),
            headers={'x-netbox-branch': 'nonexist'},
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            with apply_request_processors(request):
                self.assertIsNone(active_branch.get(), msg="A nonexistent branch was installed as active")

        reported = [str(w.message) for w in caught if 'ActiveBranchContextManager' in str(w.message)]
        self.assertEqual(reported, [], msg="An expected refusal was reported as a failed request processor")

    def test_processor_is_inert_when_app_not_installed(self):
        """
        The plugin can be imported while absent from INSTALLED_APPS: configuration.py imports
        DynamicSchemaDict from utilities, and settings.py imports the package before rejecting it
        on a version mismatch. Either way AppConfig.ready() never runs, so the deferred
        `from .models import Branch` in get_active_branch() raises at proxy model definition and
        breaks every script run. The processor must do nothing instead. See #649.

        A READY branch is used deliberately: with a nonexistent branch, get_active_branch() raises
        Branch.DoesNotExist, which the handler above already absorbs, so the assertion would pass
        with or without the guard.
        """
        branch = Branch.objects.first()
        request = RequestFactory().get(
            reverse('dcim-api:site-list'),
            headers={'x-netbox-branch': branch.schema_id},
        )

        with (
            patch('netbox_branching.utilities.apps.is_installed', return_value=False),
            ActiveBranchContextManager(request),
        ):
            self.assertIsNone(
                active_branch.get(),
                msg="A branch was activated despite netbox_branching not being installed"
            )

    def test_processor_does_not_propagate_model_import_failure(self):
        """
        Guards against the specific regression in #649: reaching get_active_branch() at all when
        the app is not installed. If the guard is ever moved or removed, this fails.
        """
        request = RequestFactory().get(reverse('dcim-api:site-list'))
        error = RuntimeError(
            "Model class netbox_branching.models.changes.ObjectChange doesn't declare an explicit "
            "app_label and isn't in an application in INSTALLED_APPS"
        )

        with (
            patch('netbox_branching.utilities.apps.is_installed', return_value=False),
            patch('netbox_branching.utilities.get_active_branch', side_effect=error),
            ActiveBranchContextManager(request),
        ):
            self.assertIsNone(active_branch.get())
