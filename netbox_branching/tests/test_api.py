import json
import uuid

from core.choices import ObjectChangeActionChoices
from core.models import Job
from dcim.models import Cable, CableTermination, Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connections
from django.test import Client, RequestFactory, TransactionTestCase
from django.urls import reverse
from netbox.context_managers import event_tracking
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


class ChangeDiffSerializerTestCase(BaseAPITestCase, TransactionTestCase):
    """
    Verify that the ChangeDiff API endpoint serializes CREATE and DELETE records
    without raising AttributeError when original or modified is None.
    """
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self.branch = Branch(name='Test Branch')
        self.branch.save(provision=False)
        self.branch.provision(self.user)

    def tearDown(self):
        connections[self.branch.connection_name].close()

    def _branch_header(self):
        return {**self.header, 'HTTP_X_NETBOX_BRANCH': self.branch.schema_id}

    def test_changediff_list_create_action(self):
        """
        Creating an object inside a branch produces a ChangeDiff with original=None.
        The changes API must return 200, not 500.
        """
        response = self.client.post(
            reverse('dcim-api:site-list'),
            data=json.dumps({'name': 'Branch Site', 'slug': 'branch-site'}),
            content_type='application/json',
            **self._branch_header(),
        )
        self.assertEqual(response.status_code, 201)

        diff = ChangeDiff.objects.get(branch=self.branch)
        self.assertEqual(diff.action, ObjectChangeActionChoices.ACTION_CREATE)
        self.assertIsNone(diff.original)

        url = reverse('plugins-api:netbox_branching-api:changediff-list')
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.content)['results'][0]
        self.assertIn('diff', result)
        self.assertEqual(result['diff'], {'original': {}, 'modified': {}, 'current': {}})

    def test_changediff_list_update_action(self):
        """
        Updating an object inside a branch produces a ChangeDiff with both original and modified
        populated. The diff field must reflect only the changed attributes.
        """
        # Site created before provisioning is present in the branch schema
        site = Site.objects.create(name='Original Site', slug='original-site')
        branch = Branch(name='Branch With Update')
        branch.save(provision=False)
        branch.provision(self.user)

        try:
            response = self.client.patch(
                reverse('dcim-api:site-detail', kwargs={'pk': site.pk}),
                data=json.dumps({'description': 'updated in branch'}),
                content_type='application/json',
                **{**self.header, 'HTTP_X_NETBOX_BRANCH': branch.schema_id},
            )
            self.assertEqual(response.status_code, 200)

            diff = ChangeDiff.objects.get(branch=branch)
            self.assertEqual(diff.action, ObjectChangeActionChoices.ACTION_UPDATE)
            self.assertIsNotNone(diff.original)
            self.assertIsNotNone(diff.modified)

            url = reverse('plugins-api:netbox_branching-api:changediff-list')
            response = self.client.get(url, **self.header)
            self.assertEqual(response.status_code, 200)
            result = json.loads(response.content)['results'][0]
            self.assertIn('diff', result)
            self.assertEqual(result['diff']['original']['description'], '')
            self.assertEqual(result['diff']['modified']['description'], 'updated in branch')
        finally:
            connections[branch.connection_name].close()

    def test_changediff_list_delete_action(self):
        """
        Deleting a pre-existing object inside a branch produces a ChangeDiff with modified=None.
        The changes API must return 200, not 500.
        """
        # Site created before provisioning is present in the branch schema
        site = Site.objects.create(name='Pre-existing Site', slug='pre-existing-site')
        branch = Branch(name='Branch With Delete')
        branch.save(provision=False)
        branch.provision(self.user)

        try:
            response = self.client.delete(
                reverse('dcim-api:site-detail', kwargs={'pk': site.pk}),
                **{**self.header, 'HTTP_X_NETBOX_BRANCH': branch.schema_id},
            )
            self.assertEqual(response.status_code, 204)

            diff = ChangeDiff.objects.get(branch=branch)
            self.assertEqual(diff.action, ObjectChangeActionChoices.ACTION_DELETE)
            self.assertIsNone(diff.modified)

            url = reverse('plugins-api:netbox_branching-api:changediff-list')
            response = self.client.get(url, **self.header)
            self.assertEqual(response.status_code, 200)
            result = json.loads(response.content)['results'][0]
            self.assertIn('diff', result)
            self.assertEqual(result['diff'], {'original': {}, 'modified': {}, 'current': {}})
        finally:
            connections[branch.connection_name].close()

    def test_changediff_list_cable_deleted_in_branch(self):
        """
        Regression test for #498: when a Cable is deleted inside a branch, the
        cascade also deletes CableTerminations whose nested serializer
        dereferences the (now-absent) Cable.  With the X-NetBox-Branch header
        active, BranchAwareRouter routes those FK lookups to the branch schema,
        where the Cable no longer exists.  The changes API must still return
        200 rather than surfacing Cable.DoesNotExist as a 500.
        """
        # Set up cable + terminations in main, before the branch is provisioned
        manufacturer = Manufacturer.objects.create(name='Mfr', slug='mfr')
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model='DT', slug='dt')
        device_role = DeviceRole.objects.create(name='Role', slug='role')
        site = Site.objects.create(name='Cable Site', slug='cable-site')
        device_a = Device.objects.create(name='A', device_type=device_type, role=device_role, site=site)
        device_b = Device.objects.create(name='B', device_type=device_type, role=device_role, site=site)
        iface_a = Interface.objects.create(device=device_a, name='eth0', type='1000base-t')
        iface_b = Interface.objects.create(device=device_b, name='eth0', type='1000base-t')
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user
        with event_tracking(request):
            cable = Cable(a_terminations=[iface_a], b_terminations=[iface_b])
            cable.save()
        cable_pk = cable.pk

        branch = Branch(name='Branch With Cable Delete')
        branch.save(provision=False)
        branch.provision(self.user)

        try:
            # Delete the cable inside the branch
            response = self.client.delete(
                reverse('dcim-api:cable-detail', kwargs={'pk': cable_pk}),
                **{**self.header, 'HTTP_X_NETBOX_BRANCH': branch.schema_id},
            )
            self.assertEqual(response.status_code, 204)

            # Query the changes API with the branch still active.  Without the
            # fix this 500s with "Cable matching query does not exist."
            url = reverse('plugins-api:netbox_branching-api:changediff-list')
            response = self.client.get(
                url,
                {'branch_id': branch.pk},
                **{**self.header, 'HTTP_X_NETBOX_BRANCH': branch.schema_id},
            )
            self.assertEqual(response.status_code, 200)

            # The CableTermination ChangeDiffs must serialize via the
            # object_repr fallback: their nested serializer would otherwise
            # dereference the (now-deleted) Cable and raise DoesNotExist.
            # Asserting `object` is a string equal to `object_repr` (rather
            # than a nested dict) proves the fallback fired.
            ct_type = ContentType.objects.get_for_model(CableTermination)
            results = json.loads(response.content)['results']
            termination_diffs = [
                r for r in results
                if r['object_type'] == f'{ct_type.app_label}.{ct_type.model}'
            ]
            self.assertEqual(len(termination_diffs), 2)
            for diff in termination_diffs:
                self.assertEqual(diff['action']['value'], ObjectChangeActionChoices.ACTION_DELETE)
                self.assertTrue(diff['object_repr'])
                self.assertEqual(diff['object'], diff['object_repr'])
        finally:
            connections[branch.connection_name].close()
