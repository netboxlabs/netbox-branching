from django.test import override_settings
from django.urls import reverse
from utilities.testing import TestCase

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import COOKIE_NAME, QUERY_PARAM
from netbox_branching.models import Branch


class RequestTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        # Create a Branch
        branch = Branch(name='Branch 1')
        branch.status = BranchStatusChoices.READY  # Fake provisioning
        branch.save(provision=False)

    @override_settings(
        LOGIN_REQUIRED=False,
        SESSION_COOKIE_DOMAIN='example.com',
        SESSION_COOKIE_PATH='/custom',
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE='Strict',
    )
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

        # Cookie attributes should mirror SESSION_COOKIE_* settings
        cookie = response.cookies[COOKIE_NAME]
        self.assertEqual(cookie['domain'], 'example.com')
        self.assertEqual(cookie['path'], '/custom')
        self.assertTrue(cookie['secure'])
        self.assertEqual(cookie['samesite'], 'Strict')

        # Verify exactly one activation toast (not duplicated by the request processor)
        messages_list = list(response.wsgi_request._messages)
        self.assertEqual(len(messages_list), 1, msg="Expected exactly one activation toast message")

    @override_settings(
        LOGIN_REQUIRED=False,
        SESSION_COOKIE_DOMAIN='example.com',
        SESSION_COOKIE_PATH='/custom',
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE='Strict',
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

        # Deletion cookie attributes should mirror SESSION_COOKIE_* settings
        cookie = response.cookies[COOKIE_NAME]
        self.assertEqual(cookie['domain'], 'example.com')
        self.assertEqual(cookie['path'], '/custom')
        self.assertEqual(cookie['samesite'], 'Strict')

    @override_settings(LOGIN_REQUIRED=False)
    def test_reactivate_branch_no_message(self):
        branch = Branch.objects.first()
        self.client.cookies.load({
            COOKIE_NAME: branch.schema_id,
        })

        url = reverse('home')
        response = self.client.get(f'{url}?{QUERY_PARAM}={branch.schema_id}')
        self.assertEqual(response.status_code, 200)
        messages_list = list(response.wsgi_request._messages)
        self.assertEqual(len(messages_list), 0, msg="Unexpected toast message on branch re-activation")

    @override_settings(LOGIN_REQUIRED=False)
    def test_stale_cookie_cleared(self):
        """
        A cookie referencing a non-ready branch should be automatically cleared.
        """
        branch = Branch.objects.first()
        branch.status = BranchStatusChoices.ARCHIVED
        branch.save(provision=False)

        self.client.cookies.load({
            COOKIE_NAME: branch.schema_id,
        })

        url = reverse('home')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, '', msg="Stale cookie was not cleared")
