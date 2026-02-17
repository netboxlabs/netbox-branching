"""
Tests for Branch merge functionality with ObjectChange collapsing.
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


class SquashMergeTestCase(TransactionTestCase):
    """Test cases for Branch merge with ObjectChange collapsing and ordering using squash strategy."""

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

        branch = Branch(name=name, merge_strategy='squash')
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
            # IMPORTANT: Call snapshot() before modifying (like views/API do)
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
        Revert: no-op since object was never created in main
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

            # IMPORTANT: Call snapshot() before modifying (like views/API do)
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

    def test_merge_delete_then_create_same_slug(self):
        """
        Test delete object, then create object with same unique constraint value (slug) with merge and revert.
        Merge: deletes old object and creates new object with same slug
        Revert: deletes new object and restores original object
        """
        # Create site in main
        site1 = Site.objects.create(name='Site 1', slug='site-1')
        site1_id = site1.id
        original_name = site1.name

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()  # Set request id for ObjectChange tracking
        request.user = self.user

        # In branch: delete old site, create new site with same slug
        with activate_branch(branch), event_tracking(request):
            Site.objects.get(id=site1_id).delete()
            site2 = Site.objects.create(name='Site 1 New', slug='site-1')
            site2_id = site2.id

        # Verify branch state
        with activate_branch(branch):
            self.assertEqual(Site.objects.count(), 1)
            self.assertEqual(Site.objects.get(id=site2_id).name, 'Site 1 New')

        # Merge branch - should succeed with new ordering
        branch.merge(user=self.user, commit=True)

        # Verify main schema
        self.assertEqual(Site.objects.count(), 1)
        site = Site.objects.get(slug='site-1')
        self.assertEqual(site.id, site2_id)
        self.assertEqual(site.name, 'Site 1 New')
        self.assertFalse(Site.objects.filter(id=site1_id).exists())

        # Verify branch status
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify revert deleted new object and restored original
        self.assertEqual(Site.objects.count(), 1)
        self.assertTrue(Site.objects.filter(id=site1_id).exists())
        self.assertFalse(Site.objects.filter(id=site2_id).exists())
        site1_restored = Site.objects.get(id=site1_id)
        self.assertEqual(site1_restored.name, original_name)
        self.assertEqual(site1_restored.slug, 'site-1')

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

    def test_merge_slug_rename_then_create(self):
        """
        Test update object's unique field, then create new object with old value with merge and revert.
        Merge: updates first object's slug and creates new object with old slug
        Revert: deletes new object and restores old slug on first object
        """
        # Create site in main
        site1 = Site.objects.create(name='Site 1', slug='site-1')
        site1_id = site1.id
        original_slug = site1.slug

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()  # Set request id for ObjectChange tracking
        request.user = self.user

        # In branch: rename site1 slug, create new site with old slug
        with activate_branch(branch), event_tracking(request):
            site1 = Site.objects.get(id=site1_id)
            # IMPORTANT: Call snapshot() before modifying (like views/API do)
            site1.snapshot()
            site1.slug = 'site-1-renamed'
            site1.save()

            site2 = Site.objects.create(name='Site 2', slug='site-1')
            site2_id = site2.id

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify main schema
        self.assertEqual(Site.objects.count(), 2)

        site1 = Site.objects.get(id=site1_id)
        self.assertEqual(site1.slug, 'site-1-renamed')

        site2 = Site.objects.get(id=site2_id)
        self.assertEqual(site2.slug, 'site-1')

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify revert deleted new object and restored old slug
        self.assertEqual(Site.objects.count(), 1)
        self.assertFalse(Site.objects.filter(id=site2_id).exists())
        site1_restored = Site.objects.get(id=site1_id)
        self.assertEqual(site1_restored.slug, original_slug)

    def test_merge_multiple_updates_collapsed(self):
        """
        Test multiple updates to same object with merge and revert.
        Merge: applies collapsed updates to object
        Revert: restores object to original state
        """
        # Create site in main
        site = Site.objects.create(name='Site 1', slug='site-1', description='Original')
        site_id = site.id
        original_name = site.name
        original_description = site.description

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()  # Set request id for ObjectChange tracking
        request.user = self.user

        # In branch: update site multiple times
        with activate_branch(branch), event_tracking(request):
            site = Site.objects.get(id=site_id)

            # IMPORTANT: Call snapshot() before modifying (like views/API do)
            site.snapshot()
            site.description = 'Update 1'
            site.save()

            site.snapshot()
            site.description = 'Update 2'
            site.save()

            site.snapshot()
            site.name = 'Site 1 Modified'
            site.save()

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify main schema - should have final state
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.name, 'Site 1 Modified')
        self.assertEqual(site.description, 'Update 2')

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify revert restored original state
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.name, original_name)
        self.assertEqual(site.description, original_description)

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

            # IMPORTANT: Call snapshot() before modifying (like views/API do)
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
            # IMPORTANT: Call snapshot() before modifying (like views/API do)
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

    def test_merge_conflicting_slug_create_update_delete(self):
        """
        Test create object with conflicting unique constraint, update it, then delete it with merge and revert.
        Merge: skips branch object (collapsed to no-op)
        Revert: no-op since object was never created in main
        """
        # Create branch
        branch = self._create_and_provision_branch()

        # In main: create site with slug that will conflict
        site_main = Site.objects.create(name='Main Site', slug='conflict-slug')
        site_main_id = site_main.id

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: create site with same slug (conflicts), update it, then delete it
        with activate_branch(branch), event_tracking(request):
            site_branch = Site.objects.create(name='Branch Site', slug='conflict-slug')
            site_branch_id = site_branch.id

            # Update description
            # IMPORTANT: Call snapshot() before modifying (like views/API do)
            site_branch.snapshot()
            site_branch.description = 'Updated in branch'
            site_branch.save()

            # Delete the site
            site_branch.delete()

        # Merge branch - should succeed (branch site is skipped)
        branch.merge(user=self.user, commit=True)

        # Verify main schema - only main site exists
        self.assertTrue(Site.objects.filter(id=site_main_id).exists())
        self.assertFalse(Site.objects.filter(id=site_branch_id).exists())
        self.assertEqual(Site.objects.filter(slug='conflict-slug').count(), 1)

        # Verify branch status
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify no changes occurred (object was never created in main)
        self.assertTrue(Site.objects.filter(id=site_main_id).exists())
        self.assertFalse(Site.objects.filter(id=site_branch_id).exists())
        self.assertEqual(Site.objects.filter(slug='conflict-slug').count(), 1)

    def test_merge_slug_update_causes_then_resolves_conflict(self):
        """
        Test create object, update to conflicting unique constraint, then update to resolve with merge and revert.
        Merge: creates object with final non-conflicting slug
        Revert: deletes the created object
        """
        # Create branch
        branch = self._create_and_provision_branch()

        # In main: create site that will have slug conflict
        site_main = Site.objects.create(name='Main Site', slug='conflict-slug')
        site_main_id = site_main.id

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: create site with non-conflicting slug, update to conflict, then resolve
        with activate_branch(branch), event_tracking(request):
            site_branch = Site.objects.create(name='Branch Site', slug='no-conflict')
            site_branch_id = site_branch.id

            # Update to conflicting slug
            # IMPORTANT: Call snapshot() before modifying (like views/API do)
            site_branch.snapshot()
            site_branch.slug = 'conflict-slug'
            site_branch.save()

            # Update again to resolve conflict
            site_branch.snapshot()
            site_branch.slug = 'resolved-slug'
            site_branch.save()

        # Merge branch - should succeed (final state has no conflict)
        branch.merge(user=self.user, commit=True)

        # Verify main schema
        self.assertTrue(Site.objects.filter(id=site_main_id).exists())
        self.assertTrue(Site.objects.filter(id=site_branch_id).exists())

        site_main = Site.objects.get(id=site_main_id)
        self.assertEqual(site_main.slug, 'conflict-slug')

        site_branch = Site.objects.get(id=site_branch_id)
        self.assertEqual(site_branch.slug, 'resolved-slug')

        # Verify branch status
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify revert deleted the created object
        self.assertTrue(Site.objects.filter(id=site_main_id).exists())
        self.assertFalse(Site.objects.filter(id=site_branch_id).exists())

    def test_merge_circuit_termination_circular_dependency(self):
        """
        Test merge with Circuit and CircuitTermination circular dependency.

        This tests the scenario where:
        - Circuit.termination_a → CircuitTermination (nullable FK)
        - CircuitTermination.circuit → Circuit (required FK)
        """
        # Create provider and circuit type in main
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        site = Site.objects.create(name='Site 1', slug='site-1')

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: create Circuit and CircuitTermination with circular dependency
        with activate_branch(branch), event_tracking(request):
            # Step 1: Create Circuit without termination_a (will be NULL initially)
            circuit = Circuit.objects.create(
                cid='TEST-001',
                provider=provider,
                type=circuit_type
            )
            circuit_id = circuit.id

            # Step 2: Create CircuitTermination pointing to the Circuit
            termination = CircuitTermination.objects.create(
                circuit=circuit,
                termination=site,
                term_side='A'
            )
            termination_id = termination.id

            # Step 3: Update Circuit to set termination_a to the CircuitTermination
            # This creates the circular reference
            circuit.snapshot()
            circuit.termination_a = termination
            circuit.save()

        # Merge branch - should succeed without circular dependency issues
        branch.merge(user=self.user, commit=True)

        # Verify main schema - both objects should exist with correct relationships
        circuit = Circuit.objects.get(id=circuit_id)
        termination = CircuitTermination.objects.get(id=termination_id)

        self.assertEqual(circuit.cid, 'TEST-001')
        self.assertEqual(circuit.termination_a_id, termination_id)
        self.assertEqual(termination.circuit_id, circuit_id)

        # Verify branch status
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)

        # Revert branch - should cleanly remove both objects
        branch.revert(user=self.user, commit=True)

        # Verify revert deleted both objects
        self.assertFalse(Circuit.objects.filter(id=circuit_id).exists())
        self.assertFalse(CircuitTermination.objects.filter(id=termination_id).exists())

    def test_merge_squash_multiple_field_changes(self):
        """
        Test that multiple changes to the same object are correctly squashed.
        Create a Site and make multiple modifications to various fields, then verify
        the final merged state reflects only the last change for each field.
        """
        # Create regions in main for testing
        region1 = Region.objects.create(name='Region 1', slug='region-1')
        region2 = Region.objects.create(name='Region 2', slug='region-2')
        region3 = Region.objects.create(name='Region 3', slug='region-3')

        # Create branch with squash strategy
        branch = self._create_and_provision_branch(name='Squash Test Branch')

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: create site and make multiple changes
        with activate_branch(branch), event_tracking(request):
            # Create the site
            site = Site.objects.create(
                name='Initial Site',
                slug='test-site',
                description='Initial description',
                physical_address='123 Initial St',
                latitude=10.0,
                longitude=20.0,
                region=region1
            )
            site_id = site.id

            # First set of updates
            site.snapshot()
            site.name = 'Updated Site 1'
            site.latitude = 11.0
            site.save()

            # Second set of updates
            site.snapshot()
            site.name = 'Updated Site 2'
            site.longitude = 21.0
            site.region = region2
            site.save()

            # Third set of updates
            site.snapshot()
            site.description = 'Updated description'
            site.latitude = 12.5
            site.save()

            # Fourth set of updates
            site.snapshot()
            site.name = 'Final Site Name'
            site.physical_address = '789 Final Ave'
            site.longitude = 22.5
            site.region = region3
            site.save()

        # Verify multiple ObjectChanges were created
        from django.contrib.contenttypes.models import ContentType
        site_ct = ContentType.objects.get_for_model(Site)
        changes = branch.get_unmerged_changes().filter(
            changed_object_type=site_ct,
            changed_object_id=site_id
        )
        # Should have 1 create + 4 updates = 5 changes
        self.assertEqual(changes.count(), 5)
        actions = [c.action for c in changes.order_by('time')]
        self.assertEqual(actions, ['create', 'update', 'update', 'update', 'update'])

        # Merge branch using squash strategy
        branch.merge(user=self.user, commit=True)

        # Verify site exists in main with final values from all changes
        self.assertTrue(Site.objects.filter(id=site_id).exists())
        merged_site = Site.objects.get(id=site_id)

        # Check that final values are correct
        self.assertEqual(merged_site.name, 'Final Site Name')  # Changed 3 times
        self.assertEqual(merged_site.slug, 'test-site')  # Never changed
        self.assertEqual(merged_site.description, 'Updated description')  # Changed once in 3rd update
        self.assertEqual(merged_site.physical_address, '789 Final Ave')  # Changed in 4th update
        self.assertEqual(merged_site.latitude, 12.5)  # Changed twice: 10.0 -> 11.0 -> 12.5
        self.assertEqual(merged_site.longitude, 22.5)  # Changed twice: 20.0 -> 21.0 -> 22.5
        self.assertEqual(merged_site.region_id, region3.id)  # Changed twice: region1 -> region2 -> region3

        # Verify branch status
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)
