import json
import uuid

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange, ObjectType
from dcim.models import Site
from netbox_branching.models import AppliedChange, Branch
from users.constants import TOKEN_PREFIX
from users.models import ObjectPermission, Token

User = get_user_model()


class ObjectChangeBranchTestCase(TestCase):
    """
    The branch a change log entry was merged from is exposed on its detail view and in GraphQL (#733).
    """

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name='Site 1', slug='site-1')
        cls.branches = Branch.objects.bulk_create((Branch(name='Branch 1'), Branch(name='Branch 2')))

        site_type = ContentType.objects.get_for_model(Site)
        cls.changes = ObjectChange.objects.bulk_create(
            ObjectChange(
                changed_object_type=site_type,
                changed_object_id=cls.site.pk,
                action=ObjectChangeActionChoices.ACTION_UPDATE,
                object_repr=str(cls.site),
                user_name='admin',
                request_id=uuid.uuid4(),
                postchange_data={'name': cls.site.name},
            )
            for _ in range(3)
        )
        # The third change was made directly in main
        AppliedChange.objects.create(change=cls.changes[0], branch=cls.branches[0])
        AppliedChange.objects.create(change=cls.changes[1], branch=cls.branches[1])

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', is_superuser=True)
        token = Token.objects.create(user=self.user)
        self.header = {'HTTP_AUTHORIZATION': f'Bearer {TOKEN_PREFIX}{token.key}.{token.token}'}

    def _graphql(self):
        query = f'{{ site(id: {self.site.pk}) {{ changelog {{ id branch {{ id name }} }} }} }}'
        response = self.client.post(
            reverse('graphql'), data={'query': query}, content_type='application/json', **self.header
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertNotIn('errors', data, data.get('errors'))
        return {int(c['id']): c['branch'] for c in data['data']['site']['changelog']}

    def test_detail_view_shows_branch(self):
        self.client.force_login(self.user)
        branch = self.branches[0]

        response = self.client.get(reverse('core:objectchange', kwargs={'pk': self.changes[0].pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, branch.get_absolute_url())

        response = self.client.get(reverse('core:objectchange', kwargs={'pk': self.changes[2].pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, branch.get_absolute_url())

    def test_graphql_branch_field(self):
        branches = self._graphql()
        self.assertEqual(
            branches,
            {
                self.changes[0].pk: {'id': str(self.branches[0].pk), 'name': 'Branch 1'},
                self.changes[1].pk: {'id': str(self.branches[1].pk), 'name': 'Branch 2'},
                self.changes[2].pk: None,
            },
        )

    def test_graphql_branch_field_requires_branch_permission(self):
        self.user.is_superuser = False
        self.user.save()
        permission = ObjectPermission.objects.create(name='View sites and changes', actions=['view'])
        permission.users.add(self.user)
        permission.object_types.add(
            ObjectType.objects.get_for_model(Site), ObjectType.objects.get_for_model(ObjectChange)
        )

        branches = self._graphql()
        self.assertEqual(set(branches), {c.pk for c in self.changes})
        self.assertEqual(set(branches.values()), {None})
