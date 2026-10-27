import uuid
import warnings
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse
from rest_framework.relations import PrimaryKeyRelatedField
from rest_framework.request import Request as DRFRequest

from core.choices import JobStatusChoices
from core.models import Job
from dcim.models import Region, Site
from extras.jobs import ScriptJob
from extras.scripts import Script
from netbox.jobs import AsyncAPIJob
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import COOKIE_NAME, QUERY_PARAM
from netbox_branching.contextvars import active_branch
from netbox_branching.middleware import BranchMiddleware
from netbox_branching.models import Branch
from netbox_branching.utilities import ActiveBranchContextManager, BranchUnavailable
from utilities.request import apply_request_processors, copy_safe_request
from utilities.testing import TestCase

from .utils import fake_request


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
        self.assertIn(COOKIE_NAME, self.client.cookies, msg='Cookie was not set on response')
        self.assertEqual(
            self.client.cookies[COOKIE_NAME].value, branch.schema_id, msg='Branch ID set in cookie is incorrect'
        )

        # Cookie attributes should mirror SESSION_COOKIE_* settings
        cookie = response.cookies[COOKIE_NAME]
        self.assertEqual(cookie['domain'], 'example.com')
        self.assertEqual(cookie['path'], '/custom')
        self.assertTrue(cookie['secure'])
        self.assertEqual(cookie['samesite'], 'Strict')

        # Verify exactly one activation toast (not duplicated by the request processor)
        messages_list = list(response.wsgi_request._messages)
        self.assertEqual(len(messages_list), 1, msg='Expected exactly one activation toast message')

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
        self.client.cookies.load(
            {
                COOKIE_NAME: branch.schema_id,
            }
        )

        # Deactivate the Branch
        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}=')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, '', msg='Cookie was not deleted')

        # Deletion cookie attributes should mirror SESSION_COOKIE_* settings
        cookie = response.cookies[COOKIE_NAME]
        self.assertEqual(cookie['domain'], 'example.com')
        self.assertEqual(cookie['path'], '/custom')
        self.assertEqual(cookie['samesite'], 'Strict')

    @override_settings(LOGIN_REQUIRED=False)
    def test_reactivate_branch_no_message(self):
        branch = Branch.objects.first()
        self.client.cookies.load(
            {
                COOKIE_NAME: branch.schema_id,
            }
        )

        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}={branch.schema_id}')
        self.assertEqual(response.status_code, 200)
        messages_list = list(response.wsgi_request._messages)
        self.assertEqual(len(messages_list), 0, msg='Unexpected toast message on branch re-activation')

    @override_settings(LOGIN_REQUIRED=False)
    def test_stale_cookie_cleared(self):
        """
        A cookie referencing a non-ready branch should be automatically cleared.
        """
        branch = Branch.objects.first()
        branch.status = BranchStatusChoices.ARCHIVED
        branch.save(provision=False, update_merge_sync_fields=True)

        self.client.cookies.load(
            {
                COOKIE_NAME: branch.schema_id,
            }
        )

        url = reverse('home')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, '', msg='Stale cookie was not cleared')

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
        response = self.client.get(reverse('api-root'), headers={'x-netbox-branch': 'nonexist'})
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
            headers={'x-netbox-branch': branch.schema_id},
        )

        self.assertEqual(response.status_code, 400)
        body = response.content.decode()
        self.assertIn('Selected branch is not ready', body)

        # The refusal is a static message: the BranchNotReady text names the branch and its status,
        # and must not be reflected back to the client (CodeQL: information exposure through an
        # exception).
        self.assertNotIn(branch.name, body, msg='Branch name leaked into the 400 response')
        self.assertNotIn(BranchStatusChoices.SYNCING, body, msg='Branch status leaked into the 400 response')

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
            self.assertIsNone(active_branch.get(), msg='An unusable branch was installed as active')

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
                self.assertIsNone(active_branch.get(), msg='A nonexistent branch was installed as active')

        reported = [str(w.message) for w in caught if 'ActiveBranchContextManager' in str(w.message)]
        self.assertEqual(reported, [], msg='An expected refusal was reported as a failed request processor')

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
                msg='A branch was activated despite netbox_branching not being installed',
            )

    def test_processor_activates_branch_from_header_on_fake_request(self):
        """
        REST API scripts run in a worker against a NetBoxFakeRequest, which has no `headers`
        attribute (#675).
        """
        branch = Branch.objects.first()
        request = fake_request(self.user, reverse('dcim-api:site-list'), headers={'x-netbox-branch': branch.schema_id})

        with ActiveBranchContextManager(request):
            self.assertEqual(active_branch.get(), branch)

    def test_processor_ignores_fake_api_request_without_header(self):
        """
        Every REST API script run takes this path, with or without a branch (#675).
        """
        request = fake_request(self.user, reverse('dcim-api:site-list'))

        with ActiveBranchContextManager(request):
            self.assertIsNone(active_branch.get())

    def test_processor_activates_branch_from_query_param_on_fake_request(self):
        """
        UI script runs carry the URL's query parameters into a NetBoxFakeRequest, which has no
        message storage (#675).
        """
        branch = Branch.objects.first()
        request = fake_request(self.user, f'{reverse("home")}?{QUERY_PARAM}={branch.schema_id}')

        with ActiveBranchContextManager(request):
            self.assertEqual(active_branch.get(), branch)

    def test_processor_refuses_unready_branch_from_query_param_on_fake_request(self):
        """
        UI script runs carry the URL's query parameters into a NetBoxFakeRequest, which has no
        message storage (#675). A browser is told and falls back to main; a job has no user to
        fall back for, so it must fail instead (#702).
        """
        branch = Branch.objects.first()
        Branch.objects.filter(pk=branch.pk).update(status=BranchStatusChoices.SYNCING)
        request = fake_request(self.user, f'{reverse("home")}?{QUERY_PARAM}={branch.schema_id}')

        with ActiveBranchContextManager(request):
            self.assertIsNone(active_branch.get())
            with self.assertRaisesMessage(BranchUnavailable, 'is not ready for use'):
                Site.objects.exists()

    def test_processor_deactivates_branch_from_query_param_on_fake_request(self):
        """
        UI script runs carry the URL's query parameters into a NetBoxFakeRequest, which has no
        message storage (#675).
        """
        request = fake_request(self.user, f'{reverse("home")}?{QUERY_PARAM}=')

        with ActiveBranchContextManager(request):
            self.assertIsNone(active_branch.get())

    def test_background_api_request_activates_branch_from_query_param(self):
        """
        apply_request_processors() only warns when a processor fails, so the background write lands
        in main (#675).
        """
        branch = Branch.objects.first()
        url = f'{reverse("dcim-api:site-list")}?{QUERY_PARAM}={branch.schema_id}&background=true'
        django_request = AsyncAPIJob._build_request(fake_request(self.user, url), {}, 'http')

        with apply_request_processors(DRFRequest(django_request)):
            self.assertEqual(active_branch.get(), branch)

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

    def assertBranchRefused(self, request, message):
        """
        Assert that a request replayed by a background job fails its first branchable query with
        `message`, leaves models which do not support branching alone, and refuses nothing once
        the request processor has exited.
        """
        with ActiveBranchContextManager(request):
            self.assertIsNone(active_branch.get(), msg='An unusable branch was installed as active')
            with self.assertRaisesMessage(BranchUnavailable, message):
                Site.objects.exists()
            Job.objects.exists()
        Site.objects.exists()

    def test_processor_refuses_deleted_branch_on_fake_request(self):
        """
        A job queued for a branch which has since been deleted must fail rather than run in main.
        Nothing refuses a replayed request as BranchMiddleware does an HTTP one, and
        apply_request_processors() swallows anything the processor raises, so the refusal surfaces
        on the first branchable query instead (#702).
        """
        request = fake_request(self.user, reverse('dcim-api:site-list'), headers={'x-netbox-branch': 'nonexist'})

        self.assertBranchRefused(request, 'Branch nonexist does not exist')

    def test_processor_refuses_unready_branch_on_fake_request(self):
        """
        A job which starts while another worker is syncing or merging its branch (#702).
        """
        branch = Branch.objects.first()
        Branch.objects.filter(pk=branch.pk).update(status=BranchStatusChoices.SYNCING)
        request = fake_request(self.user, reverse('dcim-api:site-list'), headers={'x-netbox-branch': branch.schema_id})

        self.assertBranchRefused(request, 'is not ready for use (status: syncing)')

    def test_processor_refuses_deleted_branch_from_query_param_on_fake_request(self):
        """
        A background bulk write enqueued with `?_branch=` names the branch in its query string (#702).
        """
        request = fake_request(self.user, f'{reverse("dcim-api:site-list")}?{QUERY_PARAM}=nonexist')

        self.assertBranchRefused(request, 'Branch nonexist does not exist')

    def test_processor_refuses_deleted_branch_from_cookie_on_fake_request(self):
        """
        A UI bulk operation deferred to a background job carries the branch cookie. A browser's
        stale cookie falls back to main and is cleared by BranchMiddleware; the job must fail (#702).
        """
        request = fake_request(self.user, reverse('home'), cookies={COOKIE_NAME: 'nonexist'})

        self.assertBranchRefused(request, 'Branch nonexist does not exist')

    def test_processor_refuses_unready_branch_from_cookie_on_fake_request(self):
        branch = Branch.objects.first()
        Branch.objects.filter(pk=branch.pk).update(status=BranchStatusChoices.SYNCING)
        request = fake_request(self.user, reverse('home'), cookies={COOKIE_NAME: branch.schema_id})

        self.assertBranchRefused(request, 'is not ready for use (status: syncing)')

    def test_stale_cookie_still_falls_back_to_main_for_http_request(self):
        """
        The strict refusal is for replayed requests only: an HTTP request with a stale cookie
        reaches BranchMiddleware, which falls back to main and clears the cookie (#702).
        """
        request = RequestFactory().get(reverse('home'))
        request.COOKIES[COOKIE_NAME] = 'nonexist'

        with ActiveBranchContextManager(request):
            self.assertIsNone(active_branch.get())
            Site.objects.exists()

    def test_background_api_request_refuses_deleted_branch(self):
        """
        The refusal must not be reported as a failed request processor either: that is the only
        notice apply_request_processors() gives, and the job would then run in main (#702).
        """
        url = reverse('dcim-api:site-list')
        django_request = AsyncAPIJob._build_request(
            fake_request(self.user, url, headers={'x-netbox-branch': 'nonexist'}), {}, 'http'
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            with apply_request_processors(DRFRequest(django_request)):
                self.assertIsNone(active_branch.get())
                with self.assertRaisesMessage(BranchUnavailable, 'Branch nonexist does not exist'):
                    Site.objects.exists()

        reported = [str(w.message) for w in caught if 'ActiveBranchContextManager' in str(w.message)]
        self.assertEqual(reported, [], msg='The refusal was reported as a failed request processor')

    def run_background_site_create(self, branch, payload):
        """
        Queue a background bulk create of a site for `branch`, delete the branch before a worker
        picks the job up, then run the job. Enqueued exactly as NetBoxModelViewSet does, and run
        synchronously in place of a worker.
        """
        self.add_permissions('dcim.add_site')
        request_copy = fake_request(
            self.user, reverse('dcim-api:site-list'), headers={'x-netbox-branch': branch.schema_id}
        )
        request_copy.user = None
        request_copy.COOKIES = {}
        branch.delete()

        job = AsyncAPIJob.enqueue(
            name='Bulk create sites',
            user=self.user,
            immediate=True,
            viewset_class='dcim.api.views.SiteViewSet',
            action='create',
            payload=payload,
            user_pk=self.user.pk,
            action_kwargs={},
            request=request_copy,
            scheme='http',
        )
        job.refresh_from_db()
        return job

    def test_background_api_job_fails_when_branch_deleted(self):
        """
        The scenario from #702: a background bulk write is queued for a branch, the branch is
        deleted before a worker picks the job up, and the job must fail with nothing written to main.
        """
        branch = Branch.objects.first()
        job = self.run_background_site_create(branch, {'name': 'Site 702', 'slug': 'site-702'})

        self.assertEqual(job.status, JobStatusChoices.STATUS_ERRORED)
        self.assertIn(f'Branch {branch.schema_id} does not exist', job.error)
        self.assertFalse(Site.objects.filter(name='Site 702').exists(), msg='The job wrote to main')

    def test_refusal_is_not_reported_as_missing_related_object(self):
        """
        DRF resolves a related field by catching ObjectDoesNotExist, so a refusal raised as one is
        reported as an invalid related object instead of the missing branch (#702).
        """
        region = Region.objects.create(name='Region 702', slug='region-702')
        field = PrimaryKeyRelatedField(queryset=Region.objects.all())
        request = fake_request(self.user, reverse('dcim-api:site-list'), headers={'x-netbox-branch': 'nonexist'})

        with (
            ActiveBranchContextManager(request),
            self.assertRaisesMessage(BranchUnavailable, 'Branch nonexist does not exist'),
        ):
            field.to_internal_value(region.pk)

    def test_script_job_fails_when_branch_deleted(self):
        """
        The script reproduction from #702, run through ScriptJob with the request processors
        applied as ScriptJob.run() applies them.
        """

        class CreateSite(Script):
            def run(self, data, commit):
                Site.objects.create(name='Site 702', slug='site-702')

        branch = Branch.objects.first()
        request = fake_request(
            self.user, reverse('extras-api:script-list'), headers={'x-netbox-branch': branch.schema_id}
        )
        branch.delete()
        runner = ScriptJob(Job.objects.create(name='Run Script', job_id=uuid.uuid4()))

        with (
            apply_request_processors(request),
            self.assertRaisesMessage(BranchUnavailable, f'Branch {branch.schema_id} does not exist'),
        ):
            runner.run_script(CreateSite(), request, {}, commit=True)
        self.assertFalse(Site.objects.filter(name='Site 702').exists(), msg='The script wrote to main')

    def test_middleware_drops_stale_cookie_from_request(self):
        """
        A job queued from a request which fell back to main over a stale cookie copies the
        request's cookies. The stale cookie must not reach it, or the job fails on a branch the
        user had already left (#702).
        """
        copied = {}

        def get_response(request):
            copied['request'] = copy_safe_request(request)
            return HttpResponse()

        request = RequestFactory().post(reverse('home'))
        request.user = self.user
        request.COOKIES[COOKIE_NAME] = 'nonexist'
        response = BranchMiddleware(get_response)(request)

        self.assertNotIn(COOKIE_NAME, copied['request'].COOKIES, msg='A stale cookie was copied for a job')
        self.assertEqual(response.cookies[COOKIE_NAME].value, '', msg='The stale cookie was not cleared')
        with ActiveBranchContextManager(copied['request']):
            Site.objects.exists()

    def test_middleware_keeps_valid_cookie_on_request(self):
        branch = Branch.objects.first()
        copied = {}

        def get_response(request):
            copied['request'] = copy_safe_request(request)
            return HttpResponse()

        request = RequestFactory().post(reverse('home'))
        request.user = self.user
        request.COOKIES[COOKIE_NAME] = branch.schema_id
        BranchMiddleware(get_response)(request)

        self.assertEqual(copied['request'].COOKIES.get(COOKIE_NAME), branch.schema_id)
