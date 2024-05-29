from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from dcim.models import Site
from users.models import Token

from netbox_vcs.constants import CONTEXT_HEADER
from netbox_vcs.models import Context


class APITestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        user = get_user_model().objects.create_user(username='testuser', is_superuser=True)
        Token.objects.create(user=user)

        # Create a Context
        context = Context(name='Context1')
        context.save()

        # Create sites
        Site.objects.create(name='Site 1', slug='site-1')
        Site.objects.using(context.schema_name).create(name='Site 2', slug='site-2')

    def setUp(self):
        self.client = Client()
        token = Token.objects.first()
        self.header = {
            'HTTP_AUTHORIZATION': f'Token {token.key}',
            'HTTP_ACCEPT': 'application/json',
        }

    def test_without_context(self):
        url = reverse('dcim-api:site-list')
        print('sending request')
        response = self.client.get(url, format="json", **self.header)
        print('response')
        results = response.content['results']

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Site 1')

    def test_with_context(self):
        context = Context.objects.first()
        self.header[CONTEXT_HEADER] = context.schema_id

        url = reverse('dcim-api:site-list')
        print('sending request')
        response = self.client.get(url, format="json", **self.header)
        print('response')
        results = response.content['results']

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Site 2')
