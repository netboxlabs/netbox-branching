import json

from core.choices import ObjectChangeActionChoices
from core.models import Job
from dcim.models import Site
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connections
from django.test import Client, TransactionTestCase
from django.urls import reverse
from users.models import Token

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import COOKIE_NAME
from netbox_branching.models import Branch, ChangeDiff


class BaseAPITestCase:
    serialized_rollback = True

    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_user(username='testuser', is_superuser=True)
        self.header = {
            'HTTP_AUTHORIZATION': f'Token {self.create_token(self.user)}',
            'HTTP_ACCEPT': 'application/json',
            'HTTP_CONTENT_TYPE': 'application/json',
        }
        ContentType.objects.get_for_model(Branch)

    # TODO: Remove when dropping support for NetBox v4.4
    @staticmethod
    def create_token(user):
        try:
            # NetBox >= 4.5
            from users.choices import TokenVersionChoices
            token = Token(version=TokenVersionChoices.V1, user=user)
            token.save()
        except ImportError:
            # NetBox < 4.5
            token = Token(user=user)
            token.save()
            return token.key
        else:
            return token.token


class APITestCase(BaseAPITestCase, TransactionTestCase):

    def setUp(self):
        super().setUp()

        # Create a Branch
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision(self.user)

        # Create sites
        Site.objects.create(name='Site 1', slug='site-1')
        Site.objects.using(branch.connection_name).create(name='Site 2', slug='site-2')

    def tearDown(self):
        # Manually tear down the dynamic connection created for the Branch to
        # ensure the test exits cleanly.
        branch = Branch.objects.first()
        connections[branch.connection_name].close()

    def get_results(self, response):
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        if 'results' not in data:
            raise ValueError("Response content does not contain API results")
        return data['results']

    def test_without_branch(self):
        url = reverse('dcim-api:site-list')
        response = self.client.get(url, **self.header)
        results = self.get_results(response)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Site 1')

    def test_with_branch_header(self):
        url = reverse('dcim-api:site-list')
        branch = Branch.objects.first()
        self.assertIsNotNone(branch, "Branch was not created")

        # Regular API query
        response = self.client.get(url, **self.header)
        results = self.get_results(response)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Site 1')

        # Branch-aware API query
        header = {
            **self.header,
            'HTTP_X_NETBOX_BRANCH': branch.schema_id,
        }
        response = self.client.get(url, **header)
        results = self.get_results(response)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Site 2')

    def test_with_branch_cookie(self):
        url = reverse('dcim-api:site-list')
        branch = Branch.objects.first()
        self.assertIsNotNone(branch, "Branch was not created")

        # Regular API query
        response = self.client.get(url, **self.header)
        results = self.get_results(response)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Site 1')

        # Branch-aware API query
        self.client.cookies.load({
            COOKIE_NAME: branch.schema_id,
        })
        response = self.client.get(url, **self.header)
        results = self.get_results(response)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Site 2')


class BranchArchiveAPITestCase(BaseAPITestCase, TransactionTestCase):

    def test_archive_endpoint_success(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.MERGED)
        branch.save(provision=False)
        self.assertEqual(branch.status, 'merged')

        url = reverse('plugins-api:netbox_branching-api:branch-archive', kwargs={'pk': branch.pk})
        response = self.client.post(url, **self.header)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['status']['value'], BranchStatusChoices.ARCHIVED)

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.ARCHIVED)

    def test_archive_endpoint_permission_denied(self):
        user = get_user_model().objects.create_user(username='limited_user')
        header = {
            'HTTP_AUTHORIZATION': f'Token {self.create_token(user)}',
            'HTTP_ACCEPT': 'application/json',
            'HTTP_CONTENT_TYPE': 'application/json',
        }

        branch = Branch(name='Test Branch', status=BranchStatusChoices.MERGED)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-archive', kwargs={'pk': branch.pk})
        response = self.client.post(url, **header)

        self.assertEqual(response.status_code, 403)

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)

    def test_archive_endpoint_not_mergeable(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.READY)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-archive', kwargs={'pk': branch.pk})
        response = self.client.post(url, **self.header)

        self.assertEqual(response.status_code, 400)

        branch.refresh_from_db()
        self.assertEqual(branch.status, 'ready')

    def test_patch_status_archived_blocked(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.MERGED)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-detail', kwargs={'pk': branch.pk})
        response = self.client.patch(
            url,
            data=json.dumps({'status': 'archived'}),
            content_type='application/json',
            **self.header
        )

        self.assertEqual(response.status_code, 200)

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)


class BaseBranchAPITestCase(BaseAPITestCase):
    """
    Base mixin for sync/merge/revert endpoint tests. Subclasses set:
      action        - URL action name (e.g. 'sync')
      valid_status  - branch status that allows the action
      invalid_status - branch status that should return 400
    """
    action = None
    valid_status = None
    invalid_status = None

    def get_url(self, pk):
        return reverse(f'plugins-api:netbox_branching-api:branch-{self.action}', kwargs={'pk': pk})

    def make_branch(self, status=None):
        branch = Branch(name='Test Branch', status=status or self.valid_status)
        branch.save(provision=False)
        return branch

    def test_endpoint_success(self):
        branch = self.make_branch()
        response = self.client.post(self.get_url(branch.pk), **self.header)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('status', data)
        self.assertIn('job_id', data)
        self.assertTrue(Job.objects.filter(job_id=data['job_id']).exists())

    def test_endpoint_without_commit(self):
        """Omitting 'commit' from a JSON body must not raise KeyError (issue #468)."""
        branch = self.make_branch()
        response = self.client.post(
            self.get_url(branch.pk),
            data=json.dumps({}),
            content_type='application/json',
            **self.header
        )

        self.assertEqual(response.status_code, 200)

    def test_endpoint_with_commit(self):
        branch = self.make_branch()
        response = self.client.post(
            self.get_url(branch.pk),
            data=json.dumps({'commit': True}),
            content_type='application/json',
            **self.header
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('status', data)

    def test_endpoint_permission_denied(self):
        user = get_user_model().objects.create_user(username='limited_user')
        header = {
            'HTTP_AUTHORIZATION': f'Token {self.create_token(user)}',
            'HTTP_ACCEPT': 'application/json',
            'HTTP_CONTENT_TYPE': 'application/json',
        }

        branch = self.make_branch()
        response = self.client.post(self.get_url(branch.pk), **header)

        self.assertEqual(response.status_code, 403)

    def test_endpoint_invalid_status(self):
        branch = self.make_branch(status=self.invalid_status)
        response = self.client.post(self.get_url(branch.pk), **self.header)

        self.assertEqual(response.status_code, 400)

        branch.refresh_from_db()
        self.assertEqual(branch.status, self.invalid_status)


class BranchConflictAPITestMixin:
    """
    Tests for conflict handling on sync/merge endpoints.
    """

    def make_conflict(self, branch, slug_suffix=''):
        site = Site.objects.create(name=f'Conflict Site{slug_suffix}', slug=f'conflict-site{slug_suffix}')
        ct = ContentType.objects.get_for_model(Site)
        diff = ChangeDiff(
            branch=branch,
            object_type=ct,
            object_id=site.pk,
            object_repr=str(site),
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            original={'description': ''},
            modified={'description': 'branch value'},
            current={'description': 'main value'},
        )
        diff.save()  # triggers _update_conflicts()
        return diff

    def test_conflict_returns_409(self):
        branch = self.make_branch()
        self.make_conflict(branch)

        response = self.client.post(self.get_url(branch.pk), **self.header)

        self.assertEqual(response.status_code, 409)

    def test_conflict_response_shape(self):
        branch = self.make_branch()
        diff = self.make_conflict(branch)

        response = self.client.post(self.get_url(branch.pk), **self.header)
        self.assertEqual(response.status_code, 409)
        data = json.loads(response.content)

        self.assertIn('detail', data)
        self.assertIn('conflicts', data)
        self.assertEqual(len(data['conflicts']), 1)

        conflict = data['conflicts'][0]
        self.assertEqual(conflict['id'], diff.pk)
        self.assertIn('object_type', conflict)
        self.assertIn('object_id', conflict)
        self.assertIn('object_repr', conflict)
        self.assertIn('action', conflict)
        self.assertIn('conflicts', conflict)
        self.assertIn('conflicting_data', conflict)
        self.assertIn('last_updated', conflict)

        conflicting_data = conflict['conflicting_data']
        self.assertIn('original', conflicting_data)
        self.assertIn('branch', conflicting_data)
        self.assertIn('main', conflicting_data)
        self.assertEqual(conflicting_data['original'], {'description': ''})
        self.assertEqual(conflicting_data['branch'], {'description': 'branch value'})
        self.assertEqual(conflicting_data['main'], {'description': 'main value'})

    def test_acknowledged_conflicts_proceeds(self):
        branch = self.make_branch()
        self.make_conflict(branch)

        response = self.client.post(
            self.get_url(branch.pk),
            data=json.dumps({'commit': False, 'acknowledge_conflicts': True}),
            content_type='application/json',
            **self.header
        )

        self.assertEqual(response.status_code, 200)

    def test_unacknowledged_conflicts_returns_409(self):
        branch = self.make_branch()
        self.make_conflict(branch, slug_suffix='-1')
        self.make_conflict(branch, slug_suffix='-2')

        response = self.client.post(
            self.get_url(branch.pk),
            data=json.dumps({'commit': False, 'acknowledge_conflicts': False}),
            content_type='application/json',
            **self.header
        )

        self.assertEqual(response.status_code, 409)
        data = json.loads(response.content)
        self.assertEqual(len(data['conflicts']), 2)


class BranchSyncAPITestCase(BranchConflictAPITestMixin, BaseBranchAPITestCase, TransactionTestCase):
    action = 'sync'
    valid_status = BranchStatusChoices.READY
    invalid_status = BranchStatusChoices.NEW


class BranchMergeAPITestCase(BranchConflictAPITestMixin, BaseBranchAPITestCase, TransactionTestCase):
    action = 'merge'
    valid_status = BranchStatusChoices.READY
    invalid_status = BranchStatusChoices.NEW


class BranchRevertAPITestCase(BaseBranchAPITestCase, TransactionTestCase):
    action = 'revert'
    valid_status = BranchStatusChoices.MERGED
    invalid_status = BranchStatusChoices.READY
