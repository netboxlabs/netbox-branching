import uuid
from unittest.mock import patch

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange
from dcim.models import Site
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.db import connections
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django_rq import get_queue
from utilities.testing import ViewTestCases, create_tags

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import QUERY_PARAM
from netbox_branching.models import Branch, ChangeDiff
from netbox_branching.tables import ChangesGroupedTable, ChangesTable
from netbox_branching.tests.utils import provision_branch
from netbox_branching.utilities import activate_branch
from netbox_branching.views import BaseBranchActionView, GroupedChangesViewMixin

User = get_user_model()


class BranchTestCase(ViewTestCases.PrimaryObjectViewTestCase):
    model = Branch

    def _get_base_url(self):
        viewname = super()._get_base_url()
        return f'plugins:{viewname}'

    @classmethod
    def setUpTestData(cls):

        branches = (
            Branch(name='Branch 1'),
            Branch(name='Branch 2'),
            Branch(name='Branch 3'),
        )
        Branch.objects.bulk_create(branches)

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        cls.form_data = {
            'name': 'Branch X',
            'description': 'Another branch',
            'tags': [t.pk for t in tags],
        }

        cls.csv_data = (
            "name,description",
            "Branch 4,Fourth branch",
            "Branch 5,Fifth branch",
            "Branch 6,Sixth branch",
        )

        cls.csv_update_data = (
            "id,description",
            f"{branches[0].pk},New description",
            f"{branches[1].pk},New description",
            f"{branches[2].pk},New description",
        )

        cls.bulk_edit_data = {
            'description': 'New description',
        }

    def tearDown(self):
        # Clear jobs queue
        get_queue('default').connection.flushall()


class BranchBulkMigrateViewTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_user(username='bulkmigrate_super', is_superuser=True)
        cls.unprivileged_user = User.objects.create_user(username='bulkmigrate_noperms')

        cls.pending1 = Branch(name='Pending Branch 1', status=BranchStatusChoices.PENDING_MIGRATIONS)
        cls.pending2 = Branch(name='Pending Branch 2', status=BranchStatusChoices.PENDING_MIGRATIONS)
        cls.ready = Branch(name='Ready Branch', status=BranchStatusChoices.READY)
        Branch.objects.bulk_create([cls.pending1, cls.pending2, cls.ready])

    def setUp(self):
        self.client.force_login(self.superuser)
        self.url = reverse('plugins:netbox_branching:branch_bulk_migrate')

    def tearDown(self):
        get_queue('default').connection.flushall()

    def test_confirmation_page_shows_only_pending_branches(self):
        response = self.client.post(self.url, {
            'pk': [self.pending1.pk, self.pending2.pk, self.ready.pk],
            'return_url': '/plugins/branching/branches/',
        })
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # The form's hidden pk fields should only carry pending branch PKs
        self.assertIn(f'value="{self.pending1.pk}"', content)
        self.assertIn(f'value="{self.pending2.pk}"', content)
        self.assertNotIn(f'value="{self.ready.pk}"', content)

    def test_confirm_enqueues_jobs_and_redirects(self):
        with patch('netbox_branching.views.MigrateBranchJob.enqueue') as mock_enqueue:
            response = self.client.post(self.url, {
                '_confirm': '1',
                'pk': [self.pending1.pk, self.pending2.pk],
                'return_url': '/plugins/branching/branches/',
            })
        self.assertRedirects(response, '/plugins/branching/branches/', fetch_redirect_response=False)
        self.assertEqual(mock_enqueue.call_count, 2)
        msg_texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('2' in t and 'branch' in t.lower() for t in msg_texts))

    def test_empty_selection_redirects_with_warning(self):
        response = self.client.post(self.url, {
            'pk': [self.ready.pk],
            'return_url': '/plugins/branching/branches/',
        })
        self.assertRedirects(response, '/plugins/branching/branches/', fetch_redirect_response=False)
        msg_texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('pending' in t.lower() for t in msg_texts))

    def test_permission_required(self):
        self.client.force_login(self.unprivileged_user)
        response = self.client.post(self.url, {
            'pk': [self.pending1.pk],
            'return_url': '/plugins/branching/branches/',
        })
        self.assertNotEqual(response.status_code, 200)


class BranchActionViewTestCase(TestCase):
    """
    Cover the UI confirmation views for sync / merge / revert.

    These views (BranchSyncView, BranchMergeView, BranchRevertView) all extend
    BaseBranchActionView and share GET / POST plumbing for showing the
    confirmation page and enqueuing the corresponding background job. The
    API endpoint tests in test_api.py exercise the REST path; this class
    covers the parallel UI path. Job enqueue is mocked so no schema work
    happens and the test doesn't depend on Redis being available.
    """

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_user(username='actionview_super', is_superuser=True)
        cls.unprivileged_user = User.objects.create_user(username='actionview_noperms')

        # status=NEW means get_unmerged_changes() / get_unsynced_changes() both
        # return .none(), so _get_changes_summary works without a real schema.
        cls.new_branch = Branch(name='New Branch', status=BranchStatusChoices.NEW)
        cls.new_branch.save(provision=False)

        cls.ready_branch = Branch(name='Ready Branch', status=BranchStatusChoices.READY)
        cls.ready_branch.save(provision=False)

        cls.merged_branch = Branch(name='Merged Branch', status=BranchStatusChoices.MERGED)
        cls.merged_branch.save(provision=False)

    def setUp(self):
        self.client.force_login(self.superuser)

    def tearDown(self):
        # Tests mock the *Job.enqueue calls, so nothing should land in the
        # real RQ queue — but flush it anyway to avoid leaking state if a
        # future test asserts emptiness.
        get_queue('default').connection.flushall()

    def _url(self, action, branch):
        return reverse(f'plugins:netbox_branching:branch_{action}', kwargs={'pk': branch.pk})

    # ---- sync ---------------------------------------------------------------

    def test_sync_get_renders_confirmation_page(self):
        response = self.client.get(self._url('sync', self.new_branch))
        self.assertEqual(response.status_code, 200)

    def test_sync_post_enqueues_job_and_redirects(self):
        # READY status is required by BaseBranchActionView.valid_states for sync.
        # get_unsynced_changes() for READY queries the main DB — no schema dependency.
        with patch('netbox_branching.views.SyncBranchJob.enqueue') as mock_enqueue:
            response = self.client.post(
                self._url('sync', self.ready_branch),
                data={'commit': 'on'},
            )
        mock_enqueue.assert_called_once()
        self.assertEqual(response.status_code, 302)

    def test_sync_post_with_wrong_status_shows_error(self):
        """A NEW branch must not be syncable; the error message is flashed and no job enqueued."""
        with patch('netbox_branching.views.SyncBranchJob.enqueue') as mock_enqueue:
            response = self.client.post(
                self._url('sync', self.new_branch),
                data={'commit': 'on'},
            )
        mock_enqueue.assert_not_called()
        self.assertEqual(response.status_code, 200)
        msg_texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('state' in t.lower() for t in msg_texts))

    def test_sync_requires_permission(self):
        self.client.force_login(self.unprivileged_user)
        response = self.client.post(
            self._url('sync', self.ready_branch),
            data={'commit': 'on'},
        )
        self.assertNotEqual(response.status_code, 302)

    # ---- merge --------------------------------------------------------------

    def test_merge_get_renders_confirmation_page(self):
        # NEW status keeps get_unmerged_changes() at .none(), avoiding the
        # branch-schema query path.
        response = self.client.get(self._url('merge', self.new_branch))
        self.assertEqual(response.status_code, 200)

    def test_merge_post_persists_strategy_and_enqueues_job(self):
        """
        BranchMergeView's do_action assigns form.cleaned_data['merge_strategy']
        to branch.merge_strategy and saves before enqueueing. This is the only
        action view that mutates the branch row, so the merge_strategy
        round-trip is worth a direct assertion.
        """
        # The form will call get_unmerged_changes() on render if invalid; we
        # short-circuit the schema query by patching it on the model.
        with (
            patch('netbox_branching.views.MergeBranchJob.enqueue') as mock_enqueue,
            patch.object(Branch, 'get_unmerged_changes', return_value=ObjectChange.objects.none()),
        ):
            response = self.client.post(
                self._url('merge', self.ready_branch),
                data={'commit': 'on', 'merge_strategy': 'iterative'},
            )
        mock_enqueue.assert_called_once()
        self.assertEqual(response.status_code, 302)
        self.ready_branch.refresh_from_db()
        self.assertEqual(self.ready_branch.merge_strategy, 'iterative')

    # ---- revert -------------------------------------------------------------

    def test_revert_get_renders_confirmation_page(self):
        response = self.client.get(self._url('revert', self.merged_branch))
        self.assertEqual(response.status_code, 200)

    def test_revert_post_enqueues_job_and_redirects(self):
        # Revert requires status=MERGED. get_action_summary returns None for revert,
        # so no schema queries are triggered.
        with patch('netbox_branching.views.RevertBranchJob.enqueue') as mock_enqueue:
            response = self.client.post(
                self._url('revert', self.merged_branch),
                data={'commit': 'on'},
            )
        mock_enqueue.assert_called_once()
        self.assertEqual(response.status_code, 302)

    def test_revert_post_with_wrong_status_shows_error(self):
        """A READY branch (not MERGED) must not be revertable."""
        with patch('netbox_branching.views.RevertBranchJob.enqueue') as mock_enqueue:
            response = self.client.post(
                self._url('revert', self.ready_branch),
                data={'commit': 'on'},
            )
        mock_enqueue.assert_not_called()
        self.assertEqual(response.status_code, 200)


class BranchArchiveViewTestCase(TestCase):
    """Cover BranchArchiveView: GET, POST happy path, wrong-status path."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_user(username='archiveview_super', is_superuser=True)
        cls.merged = Branch(name='Merged To Archive', status=BranchStatusChoices.MERGED)
        cls.merged.save(provision=False)
        cls.ready = Branch(name='Ready Cannot Archive', status=BranchStatusChoices.READY)
        cls.ready.save(provision=False)

    def setUp(self):
        self.client.force_login(self.superuser)
        self.url_merged = reverse(
            'plugins:netbox_branching:branch_archive', kwargs={'pk': self.merged.pk}
        )
        self.url_ready = reverse(
            'plugins:netbox_branching:branch_archive', kwargs={'pk': self.ready.pk}
        )

    def test_archive_get_renders_confirmation_for_merged_branch(self):
        response = self.client.get(self.url_merged)
        self.assertEqual(response.status_code, 200)

    def test_archive_post_archives_merged_branch(self):
        # archive() is a Branch method that sets status=ARCHIVED and deprovisions
        # the schema. We patch it so the schema-drop is a no-op for this test.
        with patch.object(Branch, 'archive') as mock_archive:
            response = self.client.post(self.url_merged, data={'confirm': 'on'})
        mock_archive.assert_called_once()
        self.assertEqual(response.status_code, 302)

    def test_archive_get_for_non_merged_branch_shows_error(self):
        """
        BranchArchiveView._validate flashes an error and returns a redirect
        when status != MERGED. The view calls self._validate but ignores its
        return value in get(), so the page still renders — but the error
        message must be present.
        """
        response = self.client.get(self.url_ready)
        msg_texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('merged' in t.lower() for t in msg_texts))


class BranchMigrateViewTestCase(TestCase):
    """Cover the single-branch BranchMigrateView (the bulk one is tested separately)."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_user(username='migrateview_super', is_superuser=True)
        cls.pending = Branch(name='Pending Migrate', status=BranchStatusChoices.PENDING_MIGRATIONS)
        cls.pending.save(provision=False)
        cls.ready = Branch(name='Ready Migrate', status=BranchStatusChoices.READY)
        cls.ready.save(provision=False)

    def setUp(self):
        self.client.force_login(self.superuser)

    def _url(self, branch):
        return reverse('plugins:netbox_branching:branch_migrate', kwargs={'pk': branch.pk})

    def test_migrate_get_renders_for_pending_branch(self):
        response = self.client.get(self._url(self.pending))
        self.assertEqual(response.status_code, 200)

    def test_migrate_post_enqueues_job_for_pending_branch(self):
        with patch('netbox_branching.views.MigrateBranchJob.enqueue') as mock_enqueue:
            response = self.client.post(self._url(self.pending), data={'confirm': 'on'})
        mock_enqueue.assert_called_once()
        self.assertEqual(response.status_code, 302)

    def test_migrate_post_with_wrong_status_shows_error(self):
        with patch('netbox_branching.views.MigrateBranchJob.enqueue') as mock_enqueue:
            response = self.client.post(self._url(self.ready), data={'confirm': 'on'})
        mock_enqueue.assert_not_called()
        self.assertEqual(response.status_code, 200)
        msg_texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('not ready' in t.lower() for t in msg_texts))


class ObjectValidationTestCase(TransactionTestCase):
    """
    Test validation behavior for operations on objects that have been deleted in main.
    Ref: Issue #422
    """
    serialized_rollback = True

    def setUp(self):
        """Set up common test data."""
        self.user = User.objects.create_user(username='testuser')

    def tearDown(self):
        """Close any branch connections that were actually opened during the test."""
        for branch in Branch.objects.all():
            if hasattr(connections._connections, branch.connection_name):
                connections[branch.connection_name].close()

    def _create_and_provision_branch(self, name='Test Branch'):
        """Helper to create and provision a branch."""
        return provision_branch(user=self.user, name=name)

    def test_edit_object_deleted_in_main_shows_error(self):
        """
        Test that editing an object in a branch that was deleted in main shows an error.
        Ref: Issue #422
        """
        # Create site in main
        site = Site.objects.create(name='Site 1', slug='site-1')
        site_id = site.id

        # Create and activate branch
        branch = self._create_and_provision_branch()

        # Delete the site in main
        site.delete()

        # Verify site is deleted in main
        self.assertFalse(Site.objects.filter(id=site_id).exists())

        # In branch: try to edit the site (should raise ValidationError)
        with activate_branch(branch):
            site_in_branch = Site.objects.get(id=site_id)
            site_in_branch.description = 'Updated description'

            with self.assertRaises(ValidationError) as cm:
                site_in_branch.full_clean()

            # Verify the error message
            error_message = str(cm.exception)
            self.assertIn('deleted in the main branch', error_message)
            self.assertIn('Cannot modify', error_message)

    def test_delete_object_deleted_in_main_no_error(self):
        """
        Test that deleting an object in a branch that was deleted in main does NOT show an error.
        Ref: Issue #422
        """
        # Create site in main
        site = Site.objects.create(name='Site 1', slug='site-1')
        site_id = site.id

        # Create and activate branch
        branch = self._create_and_provision_branch()

        # Delete the site in main
        site.delete()

        # Verify site is deleted in main
        self.assertFalse(Site.objects.filter(id=site_id).exists())

        # In branch: delete the site (should NOT raise an error)
        with activate_branch(branch):
            site_in_branch = Site.objects.get(id=site_id)
            # This should succeed without raising ValidationError or AbortRequest
            site_in_branch.delete()

        # Verify the delete succeeded
        with activate_branch(branch):
            self.assertFalse(Site.objects.filter(id=site_id).exists())

    def test_edit_object_created_in_branch_no_error(self):
        """
        Test that editing an object created within the branch does not raise an error, even before its
        CREATE ChangeDiff has been committed (i.e. it was created earlier in the same request).
        Ref: Issue #496
        """
        # Create and activate branch
        branch = self._create_and_provision_branch()

        with activate_branch(branch):
            # Create an object directly in the branch. No ObjectChange/ChangeDiff is recorded here, mirroring an
            # object created earlier in the same request whose CREATE ChangeDiff has not yet been committed.
            site = Site.objects.create(name='Branch Site', slug='branch-site')

            # Sanity check: there is no CREATE ChangeDiff for the object.
            self.assertFalse(
                ChangeDiff.objects.filter(
                    branch=branch,
                    object_id=site.pk,
                    action=ObjectChangeActionChoices.ACTION_CREATE,
                ).exists()
            )

            # Editing the branch-created object must not raise a ValidationError.
            site.description = 'Updated description'
            site.full_clean()


class BranchMiddlewareTestCase(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', is_superuser=True)
        self.client.force_login(self.user)

    def tearDown(self):
        # Manually tear down any dynamic connections created for branches
        for branch in Branch.objects.all():
            if branch.connection_name in connections:
                connections[branch.connection_name].close()

    @override_settings(LOGIN_REQUIRED=False)
    def test_redirect_on_404_during_branch_deactivation(self):
        """
        Test that deactivating a branch while viewing an object that only exists
        in that branch redirects to the dashboard with a warning message.
        """
        # Create and provision a branch
        branch = Branch(name='Test Branch')
        branch.status = BranchStatusChoices.READY
        branch.save(provision=False)
        branch.provision(user=None)

        # Create a site in the branch
        with activate_branch(branch):
            site = Site.objects.create(name='Branch Site', slug='branch-site')
            site_pk = site.pk

        # Get the URL for the site detail page
        site_url = reverse('dcim:site', kwargs={'pk': site_pk})

        # First, verify the site is accessible when the branch is active
        response = self.client.get(f'{site_url}?{QUERY_PARAM}={branch.schema_id}')
        self.assertEqual(response.status_code, 200)

        # Now deactivate the branch while viewing the site (which only exists in the branch)
        response = self.client.get(f'{site_url}?{QUERY_PARAM}=', follow=False)

        # Should redirect to the dashboard
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/')

        # Follow the redirect and check for the warning message
        response = self.client.get(f'{site_url}?{QUERY_PARAM}=', follow=True)
        messages = list(get_messages(response.wsgi_request))
        self.assertEqual(len(messages), 1)
        self.assertIn("does not exist in main", str(messages[0]))

        # Clean up
        branch.deprovision()

    @override_settings(LOGIN_REQUIRED=False)
    def test_redirect_on_404_during_branch_activation(self):
        """
        Test that activating a branch while viewing an object that only exists
        in the main branch redirects to the dashboard with a warning message.
        """
        # Create and provision a branch
        branch = Branch(name='Test Branch')
        branch.status = BranchStatusChoices.READY
        branch.save(provision=False)
        branch.provision(user=None)

        # Create a site in the main branch (not in the test branch)
        site = Site.objects.create(name='Main Site', slug='main-site')
        site_pk = site.pk

        # Get the URL for the site detail page
        site_url = reverse('dcim:site', kwargs={'pk': site_pk})

        # First, verify the site is accessible in the main branch
        response = self.client.get(site_url)
        self.assertEqual(response.status_code, 200)

        # Now activate the branch while viewing the site (which doesn't exist in the branch)
        response = self.client.get(f'{site_url}?{QUERY_PARAM}={branch.schema_id}', follow=False)

        # Should redirect to the dashboard
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/')

        # Follow the redirect and check for the warning message
        response = self.client.get(f'{site_url}?{QUERY_PARAM}={branch.schema_id}', follow=True)
        messages_list = list(get_messages(response.wsgi_request))
        warning_messages = [m for m in messages_list if 'does not exist' in str(m)]
        self.assertGreaterEqual(len(warning_messages), 1, "Expected at least one warning message")
        self.assertIn(f"branch '{branch.name}'", str(warning_messages[0]))
        self.assertIn(site_url, str(warning_messages[0]))

        # Clean up
        branch.deprovision()


class ChangesSummaryTestCase(TestCase):
    """
    Unit tests for BaseBranchActionView._get_changes_summary.
    """

    @classmethod
    def setUpTestData(cls):
        cls.site_ct = ContentType.objects.get(app_label='dcim', model='site')
        cls.device_ct = ContentType.objects.get(app_label='dcim', model='device')

    def _make_change(self, ct, obj_id, action):
        return ObjectChange.objects.create(
            request_id=uuid.uuid4(),
            action=action,
            changed_object_type=ct,
            changed_object_id=obj_id,
            user_name='testuser',
        )

    def test_empty_queryset(self):
        summary = BaseBranchActionView._get_changes_summary(ObjectChange.objects.none())
        self.assertEqual(summary['creates'], {})
        self.assertEqual(summary['updates'], {})
        self.assertEqual(summary['deletes'], {})
        self.assertEqual(summary['creates_total'], 0)
        self.assertEqual(summary['updates_total'], 0)
        self.assertEqual(summary['deletes_total'], 0)

    def test_single_create(self):
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_CREATE)
        summary = BaseBranchActionView._get_changes_summary(ObjectChange.objects.all())
        self.assertEqual(summary['creates'], {self.site_ct: 1})
        self.assertEqual(summary['updates'], {})
        self.assertEqual(summary['deletes'], {})
        self.assertEqual(summary['creates_total'], 1)

    def test_single_update(self):
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_UPDATE)
        summary = BaseBranchActionView._get_changes_summary(ObjectChange.objects.all())
        self.assertEqual(summary['creates'], {})
        self.assertEqual(summary['updates'], {self.site_ct: 1})
        self.assertEqual(summary['deletes'], {})
        self.assertEqual(summary['updates_total'], 1)

    def test_single_delete(self):
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_DELETE)
        summary = BaseBranchActionView._get_changes_summary(ObjectChange.objects.all())
        self.assertEqual(summary['creates'], {})
        self.assertEqual(summary['updates'], {})
        self.assertEqual(summary['deletes'], {self.site_ct: 1})
        self.assertEqual(summary['deletes_total'], 1)

    def test_create_then_update_counts_as_create(self):
        # Same object: create + update → counted as create, not update
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_CREATE)
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_UPDATE)
        summary = BaseBranchActionView._get_changes_summary(ObjectChange.objects.all())
        self.assertEqual(summary['creates'], {self.site_ct: 1})
        self.assertEqual(summary['updates'], {})
        self.assertEqual(summary['deletes'], {})
        self.assertEqual(summary['creates_total'], 1)

    def test_create_then_delete_counts_as_delete(self):
        # Same object: create + delete → counted as delete
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_CREATE)
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_DELETE)
        summary = BaseBranchActionView._get_changes_summary(ObjectChange.objects.all())
        self.assertEqual(summary['creates'], {})
        self.assertEqual(summary['updates'], {})
        self.assertEqual(summary['deletes'], {self.site_ct: 1})
        self.assertEqual(summary['deletes_total'], 1)

    def test_update_then_delete_counts_as_delete(self):
        # Same object: update + delete → counted as delete
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_UPDATE)
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_DELETE)
        summary = BaseBranchActionView._get_changes_summary(ObjectChange.objects.all())
        self.assertEqual(summary['creates'], {})
        self.assertEqual(summary['updates'], {})
        self.assertEqual(summary['deletes'], {self.site_ct: 1})
        self.assertEqual(summary['deletes_total'], 1)

    def test_multiple_objects_same_type(self):
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_CREATE)
        self._make_change(self.site_ct, 2, ObjectChangeActionChoices.ACTION_UPDATE)
        self._make_change(self.site_ct, 3, ObjectChangeActionChoices.ACTION_DELETE)
        summary = BaseBranchActionView._get_changes_summary(ObjectChange.objects.all())
        self.assertEqual(summary['creates'], {self.site_ct: 1})
        self.assertEqual(summary['updates'], {self.site_ct: 1})
        self.assertEqual(summary['deletes'], {self.site_ct: 1})

    def test_sorted_by_model_name(self):
        # 'device' sorts before 'site' alphabetically
        self._make_change(self.site_ct, 1, ObjectChangeActionChoices.ACTION_CREATE)
        self._make_change(self.device_ct, 1, ObjectChangeActionChoices.ACTION_CREATE)
        summary = BaseBranchActionView._get_changes_summary(ObjectChange.objects.all())
        keys = list(summary['creates'].keys())
        self.assertEqual(keys, [self.device_ct, self.site_ct])


class ChangeDiffViewTestCase(TestCase):
    """
    Cover ChangeDiffView.get_extra_context's three conditional branches:
    CREATE (original=None) skips the branch-diff computation;
    UPDATE (both original and modified set) computes the diffs;
    DELETE (modified=None) skips the branch-diff computation.
    Without these, the view's diff-rendering logic is exercised only by the
    list view, which doesn't open individual records.
    """

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_user(username='diffview_super', is_superuser=True)
        cls.branch = Branch(name='Diff View Branch', status=BranchStatusChoices.READY)
        cls.branch.save(provision=False)
        cls.site = Site.objects.create(name='Diff Site', slug='diff-site-view')
        cls.site_ct = ContentType.objects.get_for_model(Site)

    def setUp(self):
        self.client.force_login(self.superuser)

    def _make_diff(self, action, original, modified, current):
        diff = ChangeDiff(
            branch=self.branch,
            object_type=self.site_ct,
            object_id=self.site.pk,
            object_repr=str(self.site),
            action=action,
            original=original,
            modified=modified,
            current=current,
        )
        diff.save()
        return diff

    def _url(self, diff):
        return reverse('plugins:netbox_branching:changediff', args=[diff.pk])

    def test_create_diff_renders(self):
        diff = self._make_diff(
            action=ObjectChangeActionChoices.ACTION_CREATE,
            original=None,
            modified={'name': 'New', 'description': 'created in branch'},
            current=None,
        )
        response = self.client.get(self._url(diff))
        self.assertEqual(response.status_code, 200)

    def test_update_diff_renders_with_field_diffs(self):
        """Both branches of the conditional fire when original + modified + current are all present."""
        diff = self._make_diff(
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            original={'name': 'Diff Site', 'description': ''},
            modified={'name': 'Diff Site', 'description': 'changed in branch'},
            current={'name': 'Diff Site', 'description': 'changed in main'},
        )
        response = self.client.get(self._url(diff))
        self.assertEqual(response.status_code, 200)

    def test_delete_diff_renders(self):
        diff = self._make_diff(
            action=ObjectChangeActionChoices.ACTION_DELETE,
            original={'name': 'Diff Site', 'description': ''},
            modified=None,
            current=None,
        )
        response = self.client.get(self._url(diff))
        self.assertEqual(response.status_code, 200)


class GroupedChangesViewMixinTestCase(TestCase):
    """
    Unit tests for GroupedChangesViewMixin: aggregation logic and table selection.
    """

    @classmethod
    def setUpTestData(cls):
        cls.site_ct = ContentType.objects.get(app_label='dcim', model='site')
        cls.device_ct = ContentType.objects.get(app_label='dcim', model='device')

    @staticmethod
    def _make_change(request_id, ct, obj_id, action, user_name='alice'):
        return ObjectChange.objects.create(
            request_id=request_id,
            action=action,
            changed_object_type=ct,
            changed_object_id=obj_id,
            user_name=user_name,
        )

    def test_aggregate_empty_queryset(self):
        self.assertEqual(GroupedChangesViewMixin._aggregate(ObjectChange.objects.none()), [])

    def test_aggregate_counts_actions_per_group(self):
        # Single request touching one type with all three actions on different objects
        req = uuid.uuid4()
        self._make_change(req, self.site_ct, 1, ObjectChangeActionChoices.ACTION_CREATE)
        self._make_change(req, self.site_ct, 2, ObjectChangeActionChoices.ACTION_CREATE)
        self._make_change(req, self.site_ct, 3, ObjectChangeActionChoices.ACTION_UPDATE)
        self._make_change(req, self.site_ct, 4, ObjectChangeActionChoices.ACTION_DELETE)

        groups = GroupedChangesViewMixin._aggregate(ObjectChange.objects.all())

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group['request_id'], req)
        self.assertEqual(group['changed_object_type_id'], self.site_ct.id)
        self.assertEqual(group['changed_object_type'], self.site_ct)
        self.assertEqual(group['user_name'], 'alice')
        self.assertEqual(group['creates'], 2)
        self.assertEqual(group['updates'], 1)
        self.assertEqual(group['deletes'], 1)

    def test_aggregate_separates_requests_and_types(self):
        # Two requests, second one touches two types → three groups total
        req_a = uuid.uuid4()
        req_b = uuid.uuid4()
        self._make_change(req_a, self.site_ct, 1, ObjectChangeActionChoices.ACTION_CREATE)
        self._make_change(req_b, self.site_ct, 2, ObjectChangeActionChoices.ACTION_UPDATE)
        self._make_change(req_b, self.device_ct, 1, ObjectChangeActionChoices.ACTION_DELETE)

        groups = GroupedChangesViewMixin._aggregate(ObjectChange.objects.all())

        self.assertEqual(len(groups), 3)
        keys = {(g['request_id'], g['changed_object_type_id']) for g in groups}
        self.assertEqual(keys, {
            (req_a, self.site_ct.id),
            (req_b, self.site_ct.id),
            (req_b, self.device_ct.id),
        })

    def test_aggregate_resolves_content_types(self):
        # Verify ContentType lookup happens in a single batched query and attaches the object.
        req = uuid.uuid4()
        self._make_change(req, self.site_ct, 1, ObjectChangeActionChoices.ACTION_CREATE)
        self._make_change(req, self.device_ct, 1, ObjectChangeActionChoices.ACTION_CREATE)

        groups = GroupedChangesViewMixin._aggregate(ObjectChange.objects.all())

        resolved = {g['changed_object_type_id']: g['changed_object_type'] for g in groups}
        self.assertEqual(resolved[self.site_ct.id], self.site_ct)
        self.assertEqual(resolved[self.device_ct.id], self.device_ct)

    def test_is_drilldown_detects_request_id_param(self):
        factory = RequestFactory()
        self.assertTrue(GroupedChangesViewMixin._is_drilldown(factory.get('/?request_id=abc')))
        self.assertFalse(GroupedChangesViewMixin._is_drilldown(factory.get('/')))
        # Other filter params alone do not trigger drill-down
        self.assertFalse(GroupedChangesViewMixin._is_drilldown(factory.get('/?action=create')))


class BranchChangesViewTableSelectionTestCase(TestCase):
    """
    Integration tests for the branch "Changes Behind" view: confirms the grouped
    table is used by default and the flat ChangesTable is used when drilling down.
    Uses the "behind" view because its underlying queryset lives in the main DB,
    so no branch schema needs to be provisioned.
    """

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_user(username='grouped_super', is_superuser=True)
        cls.site_ct = ContentType.objects.get(app_label='dcim', model='site')

        cls.branch = Branch(name='Grouped Test Branch', status=BranchStatusChoices.READY)
        cls.branch.save(provision=False)

        # Two changes in the same request → one grouped row, two flat rows
        request_id = uuid.uuid4()
        cls.request_id = request_id
        ObjectChange.objects.create(
            request_id=request_id,
            action=ObjectChangeActionChoices.ACTION_CREATE,
            changed_object_type=cls.site_ct,
            changed_object_id=1,
            object_repr='Site 1',
            user_name='alice',
        )
        ObjectChange.objects.create(
            request_id=request_id,
            action=ObjectChangeActionChoices.ACTION_CREATE,
            changed_object_type=cls.site_ct,
            changed_object_id=2,
            object_repr='Site 2',
            user_name='alice',
        )

    def setUp(self):
        self.client.force_login(self.superuser)
        self.url = reverse('plugins:netbox_branching:branch_changes-behind', args=[self.branch.pk])

    def test_default_view_uses_grouped_table(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.context['table'], ChangesGroupedTable)
        # One grouped row covers both changes
        self.assertEqual(len(response.context['table'].rows), 1)

    def test_drilldown_uses_flat_table(self):
        response = self.client.get(f'{self.url}?request_id={self.request_id}')
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.context['table'], ChangesTable)
        # Flat table shows both raw rows
        self.assertEqual(len(response.context['table'].rows), 2)
