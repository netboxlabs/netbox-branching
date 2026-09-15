from django.contrib.auth import get_user_model
from django.test import RequestFactory
from django.test import TestCase as _TestCase
from django.urls import reverse
from users.models import ObjectPermission
from utilities.testing import TestCase

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import COOKIE_NAME, QUERY_PARAM
from netbox_branching.models import Branch
from netbox_branching.template_content import BranchSelector
from netbox_branching.utilities import get_active_branch, get_branches_for_user

User = get_user_model()


class BranchPermissionTestCase(TestCase):
    """
    Branches are restricted by NetBox's object-based permissions: a user sees and may activate only
    those branches permitted by the constraints on their view permission.
    """

    @classmethod
    def setUpTestData(cls):
        cls.other_user = User.objects.create_user(username='otheruser')

        branches = (
            Branch(name='Branch 1'),
            Branch(name='Branch 2'),
        )
        for branch in branches:
            branch.status = BranchStatusChoices.READY  # Fake provisioning
            branch.save(provision=False)

    def _constrain_to_owned_branches(self):
        """Grant view permission limited to branches owned by the test user."""
        obj_perm = ObjectPermission(name='Own branches', actions=['view'], constraints={'owner': '$user'})
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(self.branch_object_type)

    @property
    def branch_object_type(self):
        from core.models import ObjectType
        return ObjectType.objects.get_for_model(Branch)

    #
    # Queryset restriction
    #

    def test_no_permission_yields_no_branches(self):
        self.assertFalse(get_branches_for_user(self.user).exists())

    def test_constraint_limits_visible_branches(self):
        mine = Branch.objects.get(name='Branch 1')
        Branch.objects.filter(pk=mine.pk).update(owner=self.user)
        self._constrain_to_owned_branches()

        self.assertEqual([b.name for b in get_branches_for_user(self.user)], ['Branch 1'])

    #
    # Branch selector
    #

    def _render_selector(self, user):
        request = RequestFactory().get('/')
        request.user = user
        return BranchSelector(context={'request': request}).navbar()

    def test_selector_hidden_without_permission(self):
        self.assertEqual(self._render_selector(self.user), '')

    def test_selector_lists_only_permitted_branches(self):
        mine = Branch.objects.get(name='Branch 1')
        Branch.objects.filter(pk=mine.pk).update(owner=self.user)
        self._constrain_to_owned_branches()

        content = self._render_selector(self.user)
        self.assertIn('Branch 1', content)
        self.assertNotIn('Branch 2', content)

    #
    # Branch activation
    #

    def test_activation_rejected_without_permission(self):
        branch = Branch.objects.get(name='Branch 1')

        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}={branch.schema_id}')
        self.assertEqual(response.status_code, 400)

    def test_activation_rejected_for_unpermitted_branch(self):
        mine = Branch.objects.get(name='Branch 1')
        Branch.objects.filter(pk=mine.pk).update(owner=self.user)
        self._constrain_to_owned_branches()
        theirs = Branch.objects.get(name='Branch 2')

        url = reverse('home')
        self.assertEqual(self.client.get(f'{url}?{QUERY_PARAM}={mine.schema_id}').status_code, 200)
        self.assertEqual(self.client.get(f'{url}?{QUERY_PARAM}={theirs.schema_id}').status_code, 400)

    def test_cookie_for_unpermitted_branch_is_ignored(self):
        branch = Branch.objects.get(name='Branch 2')
        request = RequestFactory().get('/')
        request.user = self.user
        request.COOKIES[COOKIE_NAME] = branch.schema_id

        self.assertIsNone(get_active_branch(request))


class BulkMigratePermissionTestCase(TestCase):
    """
    The bulk migrate view resolves the submitted PKs through its restricted queryset, so a branch the
    user may not migrate cannot be queued by posting its PK directly.
    """

    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch(name='Branch 1')
        cls.branch.status = BranchStatusChoices.PENDING_MIGRATIONS  # Fake provisioning
        cls.branch.save(provision=False)

    def test_unpermitted_branch_is_rejected(self):
        from core.models import ObjectType

        obj_perm = ObjectPermission(
            name='Other branches', actions=['migrate'], constraints={'name': 'Branch 2'}
        )
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(Branch))

        url = reverse('plugins:netbox_branching:branch_bulk_migrate')
        response = self.client.post(url, data={'pk': [self.branch.pk], '_confirm': True})

        # The form rejects the PK, so the user is redirected without a job being enqueued
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.branch.jobs.exists())


class BranchAPIPermissionTestCase(_TestCase):
    """
    The REST API applies the same restriction to both the branch list and the branch action endpoints.
    """

    @classmethod
    def setUpTestData(cls):
        from core.models import ObjectType

        cls.user = User.objects.create_user(username='testuser')

        cls.mine = Branch(name='Branch 1', owner=cls.user)
        cls.mine.status = BranchStatusChoices.READY  # Fake provisioning
        cls.mine.save(provision=False)
        cls.theirs = Branch(name='Branch 2')
        cls.theirs.status = BranchStatusChoices.READY
        cls.theirs.save(provision=False)

        object_type = ObjectType.objects.get_for_model(Branch)
        obj_perm = ObjectPermission(
            name='Own branches', actions=['view', 'sync'], constraints={'owner': '$user'}
        )
        obj_perm.save()
        obj_perm.users.add(cls.user)
        obj_perm.object_types.add(object_type)

    def setUp(self):
        self.client.force_login(self.user)

    def test_list_excludes_unpermitted_branches(self):
        url = reverse('plugins-api:netbox_branching-api:branch-list')
        response = self.client.get(url, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)

        names = [b['name'] for b in response.json()['results']]
        self.assertEqual(names, ['Branch 1'])

    def test_detail_hides_unpermitted_branch(self):
        url = reverse('plugins-api:netbox_branching-api:branch-detail', kwargs={'pk': self.theirs.pk})
        response = self.client.get(url, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 404)

    def test_action_denied_on_unpermitted_branch(self):
        url = reverse('plugins-api:netbox_branching-api:branch-sync', kwargs={'pk': self.theirs.pk})
        response = self.client.post(url, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 403)
