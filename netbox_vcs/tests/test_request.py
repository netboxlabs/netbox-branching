from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_vcs.models import Context


class RequestTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        get_user_model().objects.create_user(username='testuser')

        context = Context(name='Context1')
        context.save()

    def setUp(self):
        # Initialize the test client
        self.client = Client()
        self.client.force_login(get_user_model().objects.first())

    def test_activate_context(self):
        context = Context.objects.first()

        # Activate the context
        url = reverse('home')
        response = self.client.get(f'{url}?_context={context.schema_id}')
        request = response.wsgi_request
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(request.context, msg="Context not set on request object by middleware")
        self.assertEqual(request.context, context, msg="Incorrect context set on request object")

        # Verify that the context remains active for successive requests
        response = self.client.get(url)
        request = response.wsgi_request
        self.assertIsNotNone(request.context, msg="Context not retained on successive request")
        self.assertEqual(request.context, context, msg="Incorrect context set on request object")

    def test_deactivate_context(self):
        # Attach active_context cookie to the test client
        context = Context.objects.first()
        self.client.cookies.load({
            'active_context': context.schema_id,
        })

        # Deactivate the context
        url = reverse('home')
        response = self.client.get(f'{url}?_context=')
        request = response.wsgi_request
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(request.context, msg="Context still defined on request object")
