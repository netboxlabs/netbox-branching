from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase
from django.urls import reverse

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import COOKIE_NAME, QUERY_PARAM
from netbox_branching.models import Branch


class RequestTestCase(TransactionTestCase):

    def setUp(self):
        # Initialize the test client
        user = get_user_model().objects.create_user(username='testuser')
        self.client = Client()
        self.client.force_login(user)

        # Create a Branch
        branch = Branch(name='Branch 1')
        branch.status = BranchStatusChoices.READY  # Fake provisioning
        branch.save(provision=False)

    def test_activate_branch(self):
        branch = Branch.objects.first()

        # Activate the Branch
        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}={branch.schema_id}')
        self.assertEqual(response.status_code, 200)
        self.assertIn(COOKIE_NAME, self.client.cookies, msg="Cookie was not set on response")
        self.assertEqual(
            self.client.cookies[COOKIE_NAME].value,
            branch.schema_id,
            msg="Branch ID set in cookie is incorrect"
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
