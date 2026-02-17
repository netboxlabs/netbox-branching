"""
Tests for Branch merge functionality with iterative merge strategy.
"""
import uuid

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse

from circuits.models import Circuit, CircuitTermination, CircuitType, Provider
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Region, Site
from netbox.context_managers import event_tracking
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models import Branch
from netbox_branching.utilities import activate_branch


User = get_user_model()


class IterativeMergeTestCase(TransactionTestCase):
    """Test cases for Branch merge with ObjectChange collapsing and ordering using iterative strategy."""

    serialized_rollback = True

    def setUp(self):
        """Set up common test data."""
        self.user = User.objects.create_user(username='testuser')

        # Create some base objects in main
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
        """Helper to create and provision a branch."""
        import time

        branch = Branch(name=name, merge_strategy='iterative')
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

    def test_merge_basic_create(self):
        """
        Test basic create operation with merge and revert.
        Merge: creates object in main
        Revert: deletes the created object
        """
        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: create site
        with activate_branch(branch), event_tracking(request):
            site = Site.objects.create(name='Test Site', slug='test-site')
            site_id = site.id

        # Verify ObjectChange was created in branch
        from django.contrib.contenttypes.models import ContentType
        site_ct = ContentType.objects.get_for_model(Site)
        changes = branch.get_unmerged_changes().filter(
            changed_object_type=site_ct,
            changed_object_id=site_id
        )
        self.assertEqual(changes.count(), 1)
        self.assertEqual(changes.first().action, 'create')

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify site exists in main after merge
        self.assertTrue(Site.objects.filter(id=site_id).exists())
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.name, 'Test Site')
        self.assertEqual(site.slug, 'test-site')

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify site is deleted after revert
        self.assertFalse(Site.objects.filter(id=site_id).exists())

    def test_merge_basic_update(self):
        """
        Test basic update operation with merge and revert.
        Merge: updates object in main
        Revert: restores object to original state
        """
        # Create a request context for creating the site
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # Create site in main WITH event tracking (like the real app does)
        with event_tracking(request):
            site = Site.objects.create(
                name='Original Site', slug='test-site', description='Original', custom_field_data={}
            )
        site_id = site.id
        original_description = site.description

        # Create branch
        branch = self._create_and_provision_branch()

        # In branch: update site
        with activate_branch(branch), event_tracking(request):
            site = Site.objects.get(id=site_id)
            site.snapshot()
            site.description = 'Updated'
            site.save()

        # Verify ObjectChange was created in branch
        from django.contrib.contenttypes.models import ContentType
        site_ct = ContentType.objects.get_for_model(Site)
        changes = branch.get_unmerged_changes().filter(
            changed_object_type=site_ct,
            changed_object_id=site_id
        )

        self.assertEqual(changes.count(), 1)
        self.assertEqual(changes.first().action, 'update')

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify site is updated in main after merge
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.description, 'Updated')

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify site is restored to original state after revert
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.description, original_description)

    def test_merge_basic_delete(self):
        """
        Test basic delete operation with merge and revert.
        Merge: deletes object from main
        Revert: restores the deleted object with original values
        """
        # Create site in main
        site = Site.objects.create(name='Test Site', slug='test-site')
        site_id = site.id
        original_name = site.name
        original_slug = site.slug

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: delete site
        with activate_branch(branch), event_tracking(request):
            Site.objects.get(id=site_id).delete()

        # Verify ObjectChange was created in branch
        from django.contrib.contenttypes.models import ContentType
        site_ct = ContentType.objects.get_for_model(Site)
        changes = branch.get_unmerged_changes().filter(
            changed_object_type=site_ct,
            changed_object_id=site_id
        )
        self.assertEqual(changes.count(), 1)
        self.assertEqual(changes.first().action, 'delete')

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify site is deleted in main
        self.assertFalse(Site.objects.filter(id=site_id).exists())

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify site is restored after revert
        self.assertTrue(Site.objects.filter(id=site_id).exists())
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.name, original_name)
        self.assertEqual(site.slug, original_slug)

    def test_merge_basic_create_update_delete(self):
        """
        Test create, update, then delete same object with merge and revert.
        Merge: skips object (not created in main) after collapsing
        """
        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: create, update, then delete site
        with activate_branch(branch), event_tracking(request):
            site = Site.objects.create(name='Temp Site', slug='temp-site')
            site_id = site.id

            site.snapshot()
            site.description = 'Modified'
            site.save()

            site.delete()

        # Verify 3 ObjectChanges were created in branch
        from django.contrib.contenttypes.models import ContentType
        site_ct = ContentType.objects.get_for_model(Site)
        changes = branch.get_unmerged_changes().filter(
            changed_object_type=site_ct,
            changed_object_id=site_id
        )
        self.assertEqual(changes.count(), 3)
        actions = [c.action for c in changes.order_by('time')]
        self.assertEqual(actions, ['create', 'update', 'delete'])

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify site does not exist in main (skipped during merge)
        self.assertFalse(Site.objects.filter(id=site_id).exists())

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify no changes occurred (object was never created in main)
        self.assertFalse(Site.objects.filter(id=site_id).exists())

    def test_merge_create_device_and_delete_old(self):
        """
        Test create new object, then delete old object with merge and revert.
        Merge: creates new device with interface and deletes old device with interface
        Revert: restores old device with interface and deletes new device with interface
        """
        # Create device with interface in main
        site = Site.objects.create(name='Site 1', slug='site-1')
        device_a = Device.objects.create(
            name='Device A',
            site=site,
            device_type=self.device_type,
            role=self.device_role
        )
        device_a_id = device_a.id
        device_a_name = device_a.name

        interface_a = Interface.objects.create(
            device=device_a,
            name='eth0',
            type='1000base-t'
        )
        interface_a_id = interface_a.id
        interface_a_name = interface_a.name

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()  # Set request id for ObjectChange tracking
        request.user = self.user

        # In branch: create new device with interface, delete old device with interface
        with activate_branch(branch), event_tracking(request):
            device_b = Device.objects.create(
                name='Device B',
                site=Site.objects.first(),
                device_type=DeviceType.objects.first(),
                role=DeviceRole.objects.first()
            )
            device_b_id = device_b.id

            # Create interface on new device
            interface_b = Interface.objects.create(
                device=device_b,
                name='eth0',
                type='1000base-t'
            )
            interface_b_id = interface_b.id

            # Delete old device (cascade deletes interface_a)
            Device.objects.get(id=device_a_id).delete()

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify main schema
        self.assertFalse(Device.objects.filter(id=device_a_id).exists())
        self.assertFalse(Interface.objects.filter(id=interface_a_id).exists())
        self.assertTrue(Device.objects.filter(id=device_b_id).exists())
        self.assertTrue(Interface.objects.filter(id=interface_b_id).exists())

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify revert restored old device/interface and deleted new ones
        self.assertTrue(Device.objects.filter(id=device_a_id).exists())
        self.assertTrue(Interface.objects.filter(id=interface_a_id).exists())
        self.assertFalse(Device.objects.filter(id=device_b_id).exists())
        self.assertFalse(Interface.objects.filter(id=interface_b_id).exists())

        device_a_restored = Device.objects.get(id=device_a_id)
        self.assertEqual(device_a_restored.name, device_a_name)

        interface_a_restored = Interface.objects.get(id=interface_a_id)
        self.assertEqual(interface_a_restored.name, interface_a_name)

    def test_merge_create_with_multiple_updates(self):
        """
        Test create object then update it multiple times with merge and revert.
        Merge: creates object with final state after collapsed updates
        Revert: deletes the created object
        """
        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()  # Set request id for ObjectChange tracking
        request.user = self.user

        # In branch: create site and update it multiple times
        with activate_branch(branch), event_tracking(request):
            site = Site.objects.create(name='New Site', slug='new-site', description='Initial')
            site_id = site.id

            site.snapshot()
            site.description = 'Modified 1'
            site.save()

            site.snapshot()
            site.description = 'Modified 2'
            site.name = 'New Site Final'
            site.save()

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify main schema - should have final state
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.name, 'New Site Final')
        self.assertEqual(site.description, 'Modified 2')
        self.assertEqual(site.slug, 'new-site')

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify revert deleted the created object
        self.assertFalse(Site.objects.filter(id=site_id).exists())

    def test_merge_complex_dependency_chain(self):
        """
        Test complex scenario with multiple creates, updates, and deletes with merge and revert.
        Merge: creates new devices with interfaces, updates device, and deletes old device
        Revert: deletes all created objects in reverse order and restores deleted device
        """
        # Create initial devices in main
        site = Site.objects.create(name='Site 1', slug='site-1')
        device_a = Device.objects.create(
            name='Device A',
            site=site,
            device_type=self.device_type,
            role=self.device_role
        )
        device_a_id = device_a.id
        device_a_name = device_a.name

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()  # Set request id for ObjectChange tracking
        request.user = self.user

        # In branch: complex operations
        with activate_branch(branch), event_tracking(request):
            # Create new devices
            device_b = Device.objects.create(
                name='Device B',
                site=Site.objects.first(),
                device_type=DeviceType.objects.first(),
                role=DeviceRole.objects.first()
            )
            device_b_id = device_b.id

            device_c = Device.objects.create(
                name='Device C',
                site=Site.objects.first(),
                device_type=DeviceType.objects.first(),
                role=DeviceRole.objects.first()
            )
            device_c_id = device_c.id

            # Create interface on device_b
            interface_b1 = Interface.objects.create(
                device=device_b,
                name='eth0',
                type='1000base-t'
            )
            interface_b1_id = interface_b1.id

            # Create another interface on device_b
            interface_b2 = Interface.objects.create(
                device=device_b,
                name='eth1',
                type='1000base-t'
            )
            interface_b2_id = interface_b2.id

            # Update device_b
            device_b.snapshot()
            device_b.name = 'Device B Updated'
            device_b.save()

            # Delete device_a
            Device.objects.get(id=device_a_id).delete()

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify main schema
        self.assertFalse(Device.objects.filter(id=device_a_id).exists())
        self.assertTrue(Device.objects.filter(id=device_b_id).exists())
        self.assertTrue(Device.objects.filter(id=device_c_id).exists())

        device_b = Device.objects.get(id=device_b_id)
        self.assertEqual(device_b.name, 'Device B Updated')
        self.assertEqual(device_b.interfaces.count(), 2)

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify revert deleted all created objects and restored deleted device
        self.assertTrue(Device.objects.filter(id=device_a_id).exists())
        self.assertFalse(Device.objects.filter(id=device_b_id).exists())
        self.assertFalse(Device.objects.filter(id=device_c_id).exists())
        self.assertFalse(Interface.objects.filter(id=interface_b1_id).exists())
        self.assertFalse(Interface.objects.filter(id=interface_b2_id).exists())

        device_a_restored = Device.objects.get(id=device_a_id)
        self.assertEqual(device_a_restored.name, device_a_name)
