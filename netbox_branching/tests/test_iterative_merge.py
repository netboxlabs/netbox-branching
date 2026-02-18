"""
Base merge test mixin and iterative merge tests.
"""
import uuid

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import RequestFactory
from django.urls import reverse

from netbox.context_managers import event_tracking
from netbox_branching.models import Branch
from dcim.models import DeviceRole, DeviceType, Manufacturer


User = get_user_model()


class BaseMergeTests:
    """
    Mixin with common merge tests for all merge strategies.

    This is a mixin class (not inheriting from TestCase) that provides common test methods.
    Subclasses should inherit from both this mixin and TransactionTestCase, and must
    implement _create_and_provision_branch() with their specific merge strategy.

    Example:
        class IterativeMergeTestCase(BaseMergeTests, TransactionTestCase):
            def _create_and_provision_branch(self, name='Test Branch'):
                # Implementation for iterative strategy
                ...
    """

    serialized_rollback = True

    def setUp(self):
        """Set up common test data."""
        self.user = User.objects.create_user(username='testuser')

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # Create some base objects in main
        with event_tracking(request):
            self.manufacturer = Manufacturer.objects.create(name='Manufacturer 1', slug='manufacturer-1')
            self.device_type = DeviceType.objects.create(
                manufacturer=self.manufacturer,
                model='Device Type 1',
                slug='device-type-1'
            )
            self.device_role = DeviceRole.objects.create(name='Device Role 1', slug='device-role-1')

    def tearDown(self):
        """Clean up branch connections."""
        for branch in Branch.objects.all():
            if hasattr(connections, branch.connection_name):
                connections[branch.connection_name].close()

    def _create_and_provision_branch(self, name='Test Branch'):
        """
        Helper to create and provision a branch.

        Must be implemented by subclasses to specify the merge strategy.
        """
        raise NotImplementedError("Subclasses must implement _create_and_provision_branch()")
