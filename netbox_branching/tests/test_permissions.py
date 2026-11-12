from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.context_processors import PermWrapper
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.test import RequestFactory
from django.test import TestCase as _TestCase
from django.urls import reverse

from core.choices import ObjectChangeActionChoices
from netbox_branching.choices import BranchEventTypeChoices, BranchStatusChoices
from netbox_branching.constants import COOKIE_NAME, QUERY_PARAM
from netbox_branching.models import Branch, BranchEvent, ChangeDiff
from netbox_branching.template_content import BranchSelector
from netbox_branching.utilities import get_active_branch, get_branches_for_user
from users.models import ObjectPermission
from utilities.testing import TestCase

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

    def _assert_branch_unavailable(self, response, active_branch=None):
        """
        A UI request naming a branch it may not use is not refused: the page renders in whichever branch
        was already active (main if none), with an error message which doesn't reveal whether the branch
        exists.
        """
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.active_branch, active_branch)
        self.assertIn(
            'The requested branch does not exist or you do not have permission to view it.',
            [str(message) for message in get_messages(response.wsgi_request)],
        )

    def test_activation_rejected_without_permission(self):
        branch = Branch.objects.get(name='Branch 1')

        url = reverse('home')
        self._assert_branch_unavailable(self.client.get(f'{url}?{QUERY_PARAM}={branch.schema_id}'))

    def test_activation_rejected_for_unpermitted_branch(self):
        mine = Branch.objects.get(name='Branch 1')
        Branch.objects.filter(pk=mine.pk).update(owner=self.user)
        self._constrain_to_owned_branches()
        theirs = Branch.objects.get(name='Branch 2')

        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}={mine.schema_id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.active_branch, mine)

        # Following a link to someone else's branch, or to one which doesn't exist, leaves the user in their
        # own branch rather than moving them to main
        for schema_id in (theirs.schema_id, 'nonexist'):
            with self.subTest(schema_id=schema_id):
                response = self.client.get(f'{url}?{QUERY_PARAM}={schema_id}')
                self._assert_branch_unavailable(response, active_branch=mine)
                self.assertEqual(self.client.cookies[COOKIE_NAME].value, mine.schema_id)

    def test_unpermitted_branch_is_indistinguishable_from_missing(self):
        branch = Branch.objects.get(name='Branch 1')

        url = reverse('home')
        self._assert_branch_unavailable(self.client.get(f'{url}?{QUERY_PARAM}={branch.schema_id}'))
        self._assert_branch_unavailable(self.client.get(f'{url}?{QUERY_PARAM}=nonexist'))

    def test_cookie_survives_expired_session(self):
        # An anonymous user can see no branch, but their cookie must be kept: the session may simply have expired,
        # and the branch should be active again once the user logs back in
        mine = Branch.objects.get(name='Branch 1')
        Branch.objects.filter(pk=mine.pk).update(owner=self.user)
        self._constrain_to_owned_branches()
        url = reverse('home')

        for query in ('', f'?{QUERY_PARAM}=nonexist'):
            with self.subTest(query=query):
                # Logging out discards every cookie, so restore the branch cookie as a browser would keep it
                self.client.logout()
                self.client.cookies.load({COOKIE_NAME: mine.schema_id})

                response = self.client.get(f'{url}{query}')
                self.assertIsNone(response.wsgi_request.active_branch)
                self.assertEqual(self.client.cookies[COOKIE_NAME].value, mine.schema_id)

                self.client.force_login(self.user)
                response = self.client.get(url)
                self.assertEqual(response.wsgi_request.active_branch, mine)

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

        obj_perm = ObjectPermission(name='Other branches', actions=['migrate'], constraints={'name': 'Branch 2'})
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

    def _grant(self, action, constraints):
        from core.models import ObjectType

        obj_perm = ObjectPermission(name=f'{action} constrained', actions=[action], constraints=constraints)
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(Branch))

    def test_create_must_satisfy_constraints(self):
        # Every branch created through the API satisfies the owner-scoped add permission, as its creator
        # becomes its owner, so replace that with one limited by name
        obj_perm = ObjectPermission.objects.get(name='Own branches')
        obj_perm.actions.remove('add')
        obj_perm.save()
        self._grant('add', {'name__startswith': 'Team A'})
        url = reverse('plugins-api:netbox_branching-api:branch-list')

        response = self.client.post(
            url, {'name': 'Team A branch'}, content_type='application/json', headers={'accept': 'application/json'}
        )
        self.assertEqual(response.status_code, 201)

        response = self.client.post(
            url, {'name': 'Team B branch'}, content_type='application/json', headers={'accept': 'application/json'}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Branch.objects.filter(name='Team B branch').exists())

    def test_update_must_satisfy_constraints(self):
        self._grant('change', {'name__startswith': 'Branch'})
        url = reverse('plugins-api:netbox_branching-api:branch-detail', kwargs={'pk': self.mine.pk})

        response = self.client.patch(
            url, {'description': 'Updated'}, content_type='application/json', headers={'accept': 'application/json'}
        )
        self.assertEqual(response.status_code, 200)

        # Renaming the branch would take it outside the constraint under which the user may change it
        response = self.client.patch(
            url, {'name': 'Renamed'}, content_type='application/json', headers={'accept': 'application/json'}
        )
        self.assertEqual(response.status_code, 403)
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.name, 'Branch 1')

    def test_list_excludes_unpermitted_branches(self):
        url = reverse('plugins-api:netbox_branching-api:branch-list')
        response = self.client.get(url, headers={'accept': 'application/json'})
        self.assertEqual(response.status_code, 200)

        names = [b['name'] for b in response.json()['results']]
        self.assertEqual(names, ['Branch 1'])

    def test_detail_hides_unpermitted_branch(self):
        url = reverse('plugins-api:netbox_branching-api:branch-detail', kwargs={'pk': self.theirs.pk})
        response = self.client.get(url, headers={'accept': 'application/json'})
        self.assertEqual(response.status_code, 404)

    def test_action_on_unpermitted_branch_is_indistinguishable_from_missing(self):
        # A branch outside the user's constraint must not be distinguishable from one which does not
        # exist, so both report 404 rather than 403.
        url = reverse('plugins-api:netbox_branching-api:branch-sync', kwargs={'pk': self.theirs.pk})
        self.assertEqual(self.client.post(url, headers={'accept': 'application/json'}).status_code, 404)

        url = reverse('plugins-api:netbox_branching-api:branch-sync', kwargs={'pk': 99999})
        self.assertEqual(self.client.post(url, headers={'accept': 'application/json'}).status_code, 404)

    def test_action_without_permission_is_forbidden(self):
        # The user holds no merge permission at all, which is reported as such
        url = reverse('plugins-api:netbox_branching-api:branch-merge', kwargs={'pk': self.mine.pk})
        response = self.client.post(url, headers={'accept': 'application/json'})
        self.assertEqual(response.status_code, 403)

    def test_malformed_pk_is_reported_as_missing(self):
        # The router's pk pattern admits any non-separator characters, so a value the database cannot
        # compare must be reported as a missing branch rather than raised as a server error.
        url = reverse('plugins-api:netbox_branching-api:branch-sync', kwargs={'pk': self.mine.pk})
        url = url.replace(f'/{self.mine.pk}/', '/not-a-pk/')
        self.assertEqual(self.client.post(url, headers={'accept': 'application/json'}).status_code, 404)

    #
    # Branch activation by header or query parameter
    #
    # The middleware resolves the active branch before REST framework has authenticated the request,
    # so the token is checked there to identify the user.
    #

    def _token_header(self, enabled=True):
        from users.constants import TOKEN_PREFIX
        from users.models import Token

        token = Token.objects.create(user=self.user, enabled=enabled)
        return f'Bearer {TOKEN_PREFIX}{token.key}.{token.token}'

    def test_token_request_activates_permitted_branch(self):
        self.client.logout()
        response = self.client.get(
            reverse('api-root'),
            headers={
                'accept': 'application/json',
                'authorization': self._token_header(),
                'x-netbox-branch': self.mine.schema_id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.active_branch, self.mine)

    def test_rejected_token_reports_the_authentication_failure(self):
        # A token which fails authentication leaves the user unresolved, which would make every branch
        # invisible and so report the branch as invalid. The caller must see the authentication error
        # explaining the real problem instead of a 400 blaming the branch identifier. (DRF answers 403
        # rather than 401 because NetBox's TokenAuthentication supplies no WWW-Authenticate header.)
        self.client.logout()
        response = self.client.get(
            reverse('api-root'),
            headers={
                'accept': 'application/json',
                'authorization': self._token_header(enabled=False),
                'x-netbox-branch': self.mine.schema_id,
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('Token disabled', response.json()['detail'])

    def test_rejected_token_with_query_parameter_reports_the_authentication_failure(self):
        # The query parameter must report a rejected token the same way the header does
        self.client.logout()
        response = self.client.get(
            f'{reverse("api-root")}?{QUERY_PARAM}={self.mine.schema_id}',
            headers={
                'accept': 'application/json',
                'authorization': self._token_header(enabled=False),
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('Token disabled', response.json()['detail'])

    def test_query_parameter_rejects_unpermitted_branch(self):
        # Unlike a UI request, an API request naming a branch it may not use is refused
        self.client.logout()
        response = self.client.get(
            f'{reverse("api-root")}?{QUERY_PARAM}={self.theirs.schema_id}',
            headers={
                'accept': 'application/json',
                'authorization': self._token_header(),
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_token_is_authenticated_once_per_request(self):
        # The active branch is resolved by both the request processor and BranchMiddleware, and each
        # must not authenticate the token again
        from django.contrib.auth.models import AnonymousUser

        from netbox.api.authentication import TokenAuthentication

        request = RequestFactory().get(
            reverse('api-root'),
            headers={'authorization': self._token_header(), 'x-netbox-branch': self.mine.schema_id},
        )
        request.user = AnonymousUser()
        with patch.object(
            TokenAuthentication, 'authenticate', autospec=True, side_effect=TokenAuthentication.authenticate
        ) as authenticate:
            self.assertEqual(get_active_branch(request), self.mine)
            self.assertEqual(get_active_branch(request), self.mine)
        self.assertEqual(authenticate.call_count, 1)

    def test_token_request_rejects_unpermitted_branch(self):
        self.client.logout()
        response = self.client.get(
            reverse('api-root'),
            headers={
                'accept': 'application/json',
                'authorization': self._token_header(),
                'x-netbox-branch': self.theirs.schema_id,
            },
        )
        self.assertEqual(response.status_code, 400)


class BranchDataPermissionTestCase(_TestCase):
    """
    A branch's changes and events belong to it: a user who may not view a branch must not see them either, even
    when permitted to view changes and events in general.
    """

    @classmethod
    def setUpTestData(cls):
        from core.models import ObjectType
        from dcim.models import Site

        cls.user = User.objects.create_user(username='testuser')

        cls.mine = Branch(name='Branch 1', owner=cls.user)
        cls.mine.status = BranchStatusChoices.READY  # Fake provisioning
        cls.mine.save(provision=False)
        cls.theirs = Branch(name='Branch 2')
        cls.theirs.status = BranchStatusChoices.READY
        cls.theirs.save(provision=False)

        cls.diffs = {}
        cls.events = {}
        for branch in (cls.mine, cls.theirs):
            site = Site.objects.create(name=f'Site in {branch.name}', slug=f'site-{branch.pk}')
            cls.diffs[branch] = ChangeDiff.objects.create(
                branch=branch,
                object_type=ContentType.objects.get_for_model(Site),
                object_id=site.pk,
                action=ObjectChangeActionChoices.ACTION_UPDATE,
                original={'description': ''},
                modified={'description': 'Changed'},
                current={'description': ''},
            )
            cls.events[branch] = BranchEvent.objects.create(
                branch=branch, user=cls.user, type=BranchEventTypeChoices.PROVISIONED
            )

        branch_perm = ObjectPermission(name='Own branches', actions=['view'], constraints={'owner': '$user'})
        branch_perm.save()
        branch_perm.users.add(cls.user)
        branch_perm.object_types.add(ObjectType.objects.get_for_model(Branch))

        data_perm = ObjectPermission(name='All changes and events', actions=['view'])
        data_perm.save()
        data_perm.users.add(cls.user)
        data_perm.object_types.add(
            ObjectType.objects.get_for_model(ChangeDiff), ObjectType.objects.get_for_model(BranchEvent)
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _get_api_ids(self, viewname):
        response = self.client.get(
            reverse(f'plugins-api:netbox_branching-api:{viewname}'), headers={'accept': 'application/json'}
        )
        self.assertEqual(response.status_code, 200)
        return {result['id'] for result in response.json()['results']}

    def test_api_change_list_excludes_unpermitted_branches(self):
        self.assertEqual(self._get_api_ids('changediff-list'), {self.diffs[self.mine].pk})

    def test_api_change_detail_hides_unpermitted_branch(self):
        for branch, status_code in ((self.mine, 200), (self.theirs, 404)):
            url = reverse('plugins-api:netbox_branching-api:changediff-detail', kwargs={'pk': self.diffs[branch].pk})
            self.assertEqual(self.client.get(url, headers={'accept': 'application/json'}).status_code, status_code)

    def test_api_event_list_excludes_unpermitted_branches(self):
        self.assertEqual(self._get_api_ids('branchevent-list'), {self.events[self.mine].pk})

    def test_api_event_detail_hides_unpermitted_branch(self):
        for branch, status_code in ((self.mine, 200), (self.theirs, 404)):
            url = reverse('plugins-api:netbox_branching-api:branchevent-detail', kwargs={'pk': self.events[branch].pk})
            self.assertEqual(self.client.get(url, headers={'accept': 'application/json'}).status_code, status_code)

    def test_change_list_view_excludes_unpermitted_branches(self):
        response = self.client.get(reverse('plugins:netbox_branching:changediff_list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row.record for row in response.context['table'].rows], [self.diffs[self.mine]])

    def test_change_view_hides_unpermitted_branch(self):
        self.assertEqual(self.client.get(self.diffs[self.mine].get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.get(self.diffs[self.theirs].get_absolute_url()).status_code, 404)
