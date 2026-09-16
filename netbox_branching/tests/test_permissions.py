from django.contrib.auth import get_user_model
from django.contrib.auth.context_processors import PermWrapper
from django.test import RequestFactory
from django.test import TestCase as _TestCase
from django.urls import reverse
from users.models import ObjectPermission
from utilities.testing import TestCase

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import COOKIE_NAME, QUERY_PARAM
from netbox_branching.models import Branch
from netbox_branching.template_content import BranchSelector
from netbox_branching.utilities import get_active_branch, get_branches_for_user, resolve_request_user

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
        # Mirrors the context NetBox passes to template extensions, which includes `perms`
        return BranchSelector(context={'request': request, 'perms': PermWrapper(user)}).navbar()

    def test_selector_hidden_without_permission(self):
        # The template renders only whitespace when the selector is gated out
        self.assertEqual(self._render_selector(self.user).strip(), '')

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
        # 'add' is required for any POST to the viewset: NetBox's TokenPermissions maps the method to
        # the add permission before the action's own check is reached.
        obj_perm = ObjectPermission(
            name='Own branches', actions=['view', 'add', 'sync'], constraints={'owner': '$user'}
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

    def test_action_on_unpermitted_branch_is_indistinguishable_from_missing(self):
        # A branch outside the user's constraint must not be distinguishable from one which does not
        # exist, so both report 404 rather than 403.
        url = reverse('plugins-api:netbox_branching-api:branch-sync', kwargs={'pk': self.theirs.pk})
        self.assertEqual(self.client.post(url, HTTP_ACCEPT='application/json').status_code, 404)

        url = reverse('plugins-api:netbox_branching-api:branch-sync', kwargs={'pk': 99999})
        self.assertEqual(self.client.post(url, HTTP_ACCEPT='application/json').status_code, 404)

    def test_action_without_permission_is_forbidden(self):
        # The user holds no merge permission at all, which is reported as such
        url = reverse('plugins-api:netbox_branching-api:branch-merge', kwargs={'pk': self.mine.pk})
        response = self.client.post(url, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 403)

    #
    # Branch activation by header
    #
    # The middleware resolves the active branch before REST framework has authenticated the request,
    # so request.user is still anonymous here: the permitted branches must be looked up against the
    # user identified from the token instead.
    #

    def _token_header(self):
        from users.constants import TOKEN_PREFIX
        from users.models import Token

        token = Token.objects.create(user=self.user)
        return f'Bearer {TOKEN_PREFIX}{token.key}.{token.token}'

    def test_token_request_activates_permitted_branch(self):
        self.client.logout()
        response = self.client.get(
            reverse('api-root'),
            HTTP_ACCEPT='application/json',
            HTTP_AUTHORIZATION=self._token_header(),
            HTTP_X_NETBOX_BRANCH=self.mine.schema_id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.active_branch, self.mine)

    def test_resolving_the_token_user_leaves_the_request_untouched(self):
        # DRF assigns request.user itself during view dispatch; identifying the user early must not
        # pre-empt that, so the probe is required to leave the incoming request exactly as it found it.
        from django.contrib.auth.models import AnonymousUser
        from users.constants import TOKEN_PREFIX
        from users.models import Token

        token = Token.objects.create(user=self.user)
        request = RequestFactory().get(
            reverse('api-root'),
            HTTP_AUTHORIZATION=f'Bearer {TOKEN_PREFIX}{token.key}.{token.token}',
        )
        anonymous = AnonymousUser()
        request.user = anonymous

        self.assertEqual(resolve_request_user(request), self.user)
        self.assertIs(request.user, anonymous)

    def test_token_request_rejects_unpermitted_branch(self):
        self.client.logout()
        response = self.client.get(
            reverse('api-root'),
            HTTP_ACCEPT='application/json',
            HTTP_AUTHORIZATION=self._token_header(),
            HTTP_X_NETBOX_BRANCH=self.theirs.schema_id,
        )
        self.assertEqual(response.status_code, 400)
