import json

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connections
from django.test import Client, TransactionTestCase
from django.urls import reverse

from dcim.models import Site
from users.models import Token

from netbox_branching.models import Branch


class APITestCase(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.client = Client()
        user = get_user_model().objects.create_user(username='testuser', is_superuser=True)
        token = Token(user=user)
        token.save()
        self.header = {
            'HTTP_AUTHORIZATION': f'Token {token.key}',
            'HTTP_ACCEPT': 'application/json',
            'HTTP_CONTENT_TYPE': 'application/json',
        }

        ContentType.objects.get_for_model(Branch)

        # Create a Branch
        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision()

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

    def test_with_branch(self):
        branch = Branch.objects.first()
        self.assertIsNotNone(branch, "Branch was not created")
        header = {
            **self.header,
            f'HTTP_X_NETBOX_BRANCH': branch.schema_id,
        }

        # Sanity checks
        self.assertEqual(Site.objects.count(), 1)
        self.assertEqual(Site.objects.using(branch.connection_name).count(), 1)

        url = reverse('dcim-api:site-list')
        response = self.client.get(url, **header)
        results = self.get_results(response)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Site 2')
