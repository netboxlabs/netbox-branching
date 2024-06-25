from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_branching.constants import COOKIE_NAME, QUERY_PARAM
from netbox_branching.models import Branch


class RequestTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        get_user_model().objects.create_user(username='testuser')

        branch = Branch(name='Branch 1')
        branch.save(provision=False)
        branch.provision()

    def setUp(self):
        # Initialize the test client
        self.client = Client()
        self.client.force_login(get_user_model().objects.first())

    def test_activate_branch(self):
        branch = Branch.objects.first()

        # Activate the Branch
        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}={branch.schema_id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.cookies[COOKIE_NAME].value,
            branch.schema_id,
            msg="Cookie was not set on response"
        )

    def test_deactivate_branch(self):
        # Attach the cookie to the test client
        branch = Branch.objects.first()
        self.client.cookies.load({
            COOKIE_NAME: branch.schema_id,
        })

        # Deactivate the Branch
        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}=')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, '', msg="Cookie was not deleted")
