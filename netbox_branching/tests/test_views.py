from django.contrib.messages import get_messages
from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django_rq import get_queue

from dcim.models import Site
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import QUERY_PARAM
from netbox_branching.models import Branch
from netbox_branching.utilities import activate_branch
from utilities.testing import ViewTestCases, create_tags


class BranchTestCase(ViewTestCases.PrimaryObjectViewTestCase):
    model = Branch

    def _get_base_url(self):
        viewname = super()._get_base_url()
        return f'plugins:{viewname}'

    @classmethod
    def setUpTestData(cls):

        branches = (
            Branch(name='Branch 1'),
            Branch(name='Branch 2'),
            Branch(name='Branch 3'),
        )
        Branch.objects.bulk_create(branches)

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        cls.form_data = {
            'name': 'Branch X',
            'description': 'Another branch',
            'tags': [t.pk for t in tags],
        }

        cls.csv_data = (
            "name,description",
            "Branch 4,Fourth branch",
            "Branch 5,Fifth branch",
            "Branch 6,Sixth branch",
        )

        cls.csv_update_data = (
            "id,description",
            f"{branches[0].pk},New description",
            f"{branches[1].pk},New description",
            f"{branches[2].pk},New description",
        )

        cls.bulk_edit_data = {
            'description': 'New description',
        }

    def tearDown(self):
        # Clear jobs queue
        get_queue('default').connection.flushall()


class BranchMiddlewareTestCase(TransactionTestCase):
    serialized_rollback = True

    def tearDown(self):
        # Manually tear down any dynamic connections created for branches
        for branch in Branch.objects.all():
            if branch.connection_name in connections:
                connections[branch.connection_name].close()

    @override_settings(LOGIN_REQUIRED=False)
    def test_redirect_on_404_during_branch_deactivation(self):
        """
        Test that deactivating a branch while viewing an object that only exists
        in that branch redirects to the dashboard with a warning message.
        """
        # Create and provision a branch
        branch = Branch(name='Test Branch')
        branch.status = BranchStatusChoices.READY
        branch.save(provision=False)
        branch.provision(user=None)

        # Create a site in the branch
        with activate_branch(branch):
            site = Site.objects.create(name='Branch Site', slug='branch-site')
            site_pk = site.pk

        # Get the URL for the site detail page
        site_url = reverse('dcim:site', kwargs={'pk': site_pk})

        # First, verify the site is accessible when the branch is active
        response = self.client.get(f'{site_url}?{QUERY_PARAM}={branch.schema_id}')
        self.assertEqual(response.status_code, 200)

        # Now deactivate the branch while viewing the site (which only exists in the branch)
        response = self.client.get(f'{site_url}?{QUERY_PARAM}=', follow=False)

        # Should redirect to the dashboard
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/')

        # Follow the redirect and check for the warning message
        response = self.client.get(f'{site_url}?{QUERY_PARAM}=', follow=True)
        messages = list(get_messages(response.wsgi_request))
        self.assertEqual(len(messages), 1)
        self.assertEqual(str(messages[0]), "The requested object does not exist in the current branch.")

        # Clean up
        branch.deprovision()
