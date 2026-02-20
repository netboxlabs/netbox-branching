import json

from dcim.models import Site
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connections
from django.test import Client, TransactionTestCase
from django.urls import reverse
from users.choices import TokenVersionChoices
from users.models import Token

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import COOKIE_NAME
from netbox_branching.models import Branch


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

    def create_token(self, user):
        token = Token(version=TokenVersionChoices.V1, user=user)
        token.save()
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


class BranchSyncAPITestCase(BaseAPITestCase, TransactionTestCase):

    def test_sync_endpoint_success(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.READY)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-sync', kwargs={'pk': branch.pk})
        response = self.client.post(url, **self.header)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('status', data)

    def test_sync_endpoint_with_commit(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.READY)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-sync', kwargs={'pk': branch.pk})
        response = self.client.post(
            url,
            data=json.dumps({'commit': True}),
            content_type='application/json',
            **self.header
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('status', data)

    def test_sync_endpoint_permission_denied(self):
        user = get_user_model().objects.create_user(username='limited_user')
        header = {
            'HTTP_AUTHORIZATION': f'Token {self.create_token(user)}',
            'HTTP_ACCEPT': 'application/json',
            'HTTP_CONTENT_TYPE': 'application/json',
        }

        branch = Branch(name='Test Branch', status=BranchStatusChoices.READY)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-sync', kwargs={'pk': branch.pk})
        response = self.client.post(url, **header)

        self.assertEqual(response.status_code, 403)

    def test_sync_endpoint_not_ready(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.NEW)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-sync', kwargs={'pk': branch.pk})
        response = self.client.post(url, **self.header)

        self.assertEqual(response.status_code, 400)

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.NEW)


class BranchMergeAPITestCase(BaseAPITestCase, TransactionTestCase):

    def test_merge_endpoint_success(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.READY)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-merge', kwargs={'pk': branch.pk})
        response = self.client.post(url, **self.header)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('status', data)

    def test_merge_endpoint_with_commit(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.READY)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-merge', kwargs={'pk': branch.pk})
        response = self.client.post(
            url,
            data=json.dumps({'commit': True}),
            content_type='application/json',
            **self.header
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('status', data)

    def test_merge_endpoint_permission_denied(self):
        user = get_user_model().objects.create_user(username='limited_user')
        header = {
            'HTTP_AUTHORIZATION': f'Token {self.create_token(user)}',
            'HTTP_ACCEPT': 'application/json',
            'HTTP_CONTENT_TYPE': 'application/json',
        }

        branch = Branch(name='Test Branch', status=BranchStatusChoices.READY)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-merge', kwargs={'pk': branch.pk})
        response = self.client.post(url, **header)

        self.assertEqual(response.status_code, 403)

    def test_merge_endpoint_not_ready(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.NEW)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-merge', kwargs={'pk': branch.pk})
        response = self.client.post(url, **self.header)

        self.assertEqual(response.status_code, 400)

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.NEW)


class BranchRevertAPITestCase(BaseAPITestCase, TransactionTestCase):

    def test_revert_endpoint_success(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.MERGED)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-revert', kwargs={'pk': branch.pk})
        response = self.client.post(url, **self.header)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('status', data)

    def test_revert_endpoint_with_commit(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.MERGED)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-revert', kwargs={'pk': branch.pk})
        response = self.client.post(
            url,
            data=json.dumps({'commit': True}),
            content_type='application/json',
            **self.header
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('status', data)

    def test_revert_endpoint_permission_denied(self):
        user = get_user_model().objects.create_user(username='limited_user')
        header = {
            'HTTP_AUTHORIZATION': f'Token {self.create_token(user)}',
            'HTTP_ACCEPT': 'application/json',
            'HTTP_CONTENT_TYPE': 'application/json',
        }

        branch = Branch(name='Test Branch', status=BranchStatusChoices.MERGED)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-revert', kwargs={'pk': branch.pk})
        response = self.client.post(url, **header)

        self.assertEqual(response.status_code, 403)

    def test_revert_endpoint_not_merged(self):
        branch = Branch(name='Test Branch', status=BranchStatusChoices.READY)
        branch.save(provision=False)

        url = reverse('plugins-api:netbox_branching-api:branch-revert', kwargs={'pk': branch.pk})
        response = self.client.post(url, **self.header)

        self.assertEqual(response.status_code, 400)

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)
