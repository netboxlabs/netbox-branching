from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connections
from django.test import TransactionTestCase
from django_rq import get_queue

from dcim.models import Site
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models import Branch
from netbox_branching.utilities import activate_branch
from utilities.exceptions import AbortRequest
from utilities.testing import ViewTestCases, create_tags


User = get_user_model()


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


class ObjectValidationTestCase(TransactionTestCase):
    """
    Test validation behavior for operations on objects that have been deleted in main.
    Ref: Issue #422
    """
    serialized_rollback = True

    def setUp(self):
        """Set up common test data."""
        self.user = User.objects.create_user(username='testuser')

    def tearDown(self):
        """Clean up branch connections."""
        for branch in Branch.objects.all():
            if hasattr(connections, branch.connection_name):
                connections[branch.connection_name].close()

    def _create_and_provision_branch(self, name='Test Branch'):
        """Helper to create and provision a branch."""
        import time

        branch = Branch(name=name)
        branch.save(provision=False)
        branch.provision(user=self.user)

        # Wait for branch to be provisioned (background task)
        max_wait = 30  # Maximum 30 seconds
        wait_interval = 0.1  # Check every 100ms
        elapsed = 0

        while elapsed < max_wait:
            branch.refresh_from_db()
            if branch.status == BranchStatusChoices.READY:
                break
            time.sleep(wait_interval)
            elapsed += wait_interval
        else:
            raise TimeoutError(
                f"Branch {branch.name} did not become READY within {max_wait} seconds. "
                f"Status: {branch.status}"
            )

        return branch

    def test_edit_object_deleted_in_main_shows_error(self):
        """
        Test that editing an object in a branch that was deleted in main shows an error.
        Ref: Issue #422
        """
        # Create site in main
        site = Site.objects.create(name='Site 1', slug='site-1')
        site_id = site.id

        # Create and activate branch
        branch = self._create_and_provision_branch()

        # Delete the site in main
        site.delete()

        # Verify site is deleted in main
        self.assertFalse(Site.objects.filter(id=site_id).exists())

        # In branch: try to edit the site (should raise ValidationError)
        with activate_branch(branch):
            site_in_branch = Site.objects.get(id=site_id)
            site_in_branch.description = 'Updated description'

            with self.assertRaises(ValidationError) as cm:
                site_in_branch.full_clean()

            # Verify the error message
            error_message = str(cm.exception)
            self.assertIn('deleted in the main branch', error_message)
            self.assertIn('Cannot modify', error_message)

    def test_delete_object_deleted_in_main_no_error(self):
        """
        Test that deleting an object in a branch that was deleted in main does NOT show an error.
        Ref: Issue #422
        """
        # Create site in main
        site = Site.objects.create(name='Site 1', slug='site-1')
        site_id = site.id

        # Create and activate branch
        branch = self._create_and_provision_branch()

        # Delete the site in main
        site.delete()

        # Verify site is deleted in main
        self.assertFalse(Site.objects.filter(id=site_id).exists())

        # In branch: delete the site (should NOT raise an error)
        with activate_branch(branch):
            site_in_branch = Site.objects.get(id=site_id)
            # This should succeed without raising ValidationError or AbortRequest
            site_in_branch.delete()

        # Verify the delete succeeded
        with activate_branch(branch):
            self.assertFalse(Site.objects.filter(id=site_id).exists())
