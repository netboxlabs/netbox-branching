from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_vcs.choices import ContextStatusChoices
from netbox_vcs.contextvars import active_context
from netbox_vcs.models import Context


class RequestTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        get_user_model().objects.create_user(username='testuser')

        context = Context(name='Context1')
        context.save()
        context.provision()

    def setUp(self):
        # Initialize the test client
        self.client = Client()
        self.client.force_login(get_user_model().objects.first())

    def test_activate_context(self):
        context = Context.objects.first()

        # Activate the context
        url = reverse('home')
        response = self.client.get(f'{url}?_context={context.schema_id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.cookies['active_context'].value,
            context.schema_id,
            msg="Cookie was not set on response"
        )

    def test_deactivate_context(self):
        # Attach active_context cookie to the test client
        context = Context.objects.first()
        self.client.cookies.load({
            'active_context': context.schema_id,
        })

        # Deactivate the context
        url = reverse('home')
        response = self.client.get(f'{url}?_context=')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.cookies['active_context'].value, '', msg="Cookie was not deleted")
