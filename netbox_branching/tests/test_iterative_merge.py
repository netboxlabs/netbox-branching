"""
Tests for Branch merge functionality with common base class and iterative merge strategy.
"""
import time
import uuid

from dcim.models import (
    Cable,
    CablePath,
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Region,
    Site,
    VirtualChassis,
)
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connections
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse
from extras.models import Tag
from netbox.context_managers import event_tracking

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models import Branch, ChangeDiff
from netbox_branching.utilities import activate_branch

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

    def _assert_object_changes(self, branch, model, object_id, expected_count, expected_actions=None):
        """
        Helper to verify ObjectChanges for an object in a branch.

        Args:
            branch: The Branch to check
            model: The model class (e.g., Site)
            object_id: The object's primary key
            expected_count: Expected number of changes
            expected_actions: Optional list of expected actions in order (e.g., ['create'], ['update', 'delete'])

        Returns:
            QuerySet of changes
        """
        content_type = ContentType.objects.get_for_model(model)
        changes = branch.get_unmerged_changes().filter(
            changed_object_type=content_type,
            changed_object_id=object_id
        ).order_by('time')
        self.assertEqual(changes.count(), expected_count)

        if expected_actions:
            actual_actions = [c.action for c in changes]
            self.assertEqual(actual_actions, expected_actions)

        return changes

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
        self._assert_object_changes(branch, Site, site_id, 1, ['create'])

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
        self._assert_object_changes(branch, Site, site_id, 1, ['update'])

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
        self._assert_object_changes(branch, Site, site_id, 1, ['delete'])

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
        self._assert_object_changes(branch, Site, site_id, 3, ['create', 'update', 'delete'])

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

    def test_merge_multiple_independent_objects(self):
        """
        Test creating, updating, and deleting multiple independent objects.
        Verifies that merge handles parallel changes to unrelated objects correctly.
        """
        # Create some objects in main
        site_a = Site.objects.create(name='Site A', slug='site-a')
        site_a_id = site_a.id
        site_c = Site.objects.create(name='Site C', slug='site-c')
        site_c_id = site_c.id

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: multiple independent operations
        with activate_branch(branch), event_tracking(request):
            # Create new Site B
            site_b = Site.objects.create(name='Site B', slug='site-b')
            site_b_id = site_b.id

            # Update existing Site A
            site_a = Site.objects.get(id=site_a_id)
            site_a.snapshot()
            site_a.description = 'Updated Site A'
            site_a.save()

            # Delete existing Site C
            Site.objects.get(id=site_c_id).delete()

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify all operations completed correctly
        # Site B should be created
        self.assertTrue(Site.objects.filter(id=site_b_id).exists())
        site_b = Site.objects.get(id=site_b_id)
        self.assertEqual(site_b.name, 'Site B')

        # Site A should be updated
        self.assertTrue(Site.objects.filter(id=site_a_id).exists())
        site_a = Site.objects.get(id=site_a_id)
        self.assertEqual(site_a.description, 'Updated Site A')

        # Site C should be deleted
        self.assertFalse(Site.objects.filter(id=site_c_id).exists())

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify revert restores everything
        self.assertFalse(Site.objects.filter(id=site_b_id).exists())
        site_a = Site.objects.get(id=site_a_id)
        self.assertEqual(site_a.description, '')  # Back to original
        self.assertTrue(Site.objects.filter(id=site_c_id).exists())

    def test_merge_fk_nullification_before_delete(self):
        """
        Test setting FK to NULL, then deleting the referenced object.
        Verifies that merge properly orders FK cleanup before deletion.
        """
        # Create region and site in main
        region = Region.objects.create(name='Test Region', slug='test-region')
        region_id = region.id

        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        with event_tracking(request):
            site = Site.objects.create(name='Test Site', slug='test-site', region=region)
        site_id = site.id

        # Create branch
        branch = self._create_and_provision_branch()

        # In branch: nullify FK, then delete region
        with activate_branch(branch), event_tracking(request):
            # Update site to remove region reference
            site = Site.objects.get(id=site_id)
            site.snapshot()
            site.region = None
            site.save()

            # Delete the region
            Region.objects.get(id=region_id).delete()

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify site exists with NULL region
        site = Site.objects.get(id=site_id)
        self.assertIsNone(site.region)

        # Verify region is deleted
        self.assertFalse(Region.objects.filter(id=region_id).exists())

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify region is restored and site FK is restored
        self.assertTrue(Region.objects.filter(id=region_id).exists())
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.region_id, region_id)

    def test_merge_hierarchical_mptt_structure(self):
        """
        Test updating MPTT hierarchical structures with multi-level parent/child relationships.
        Creates a deep hierarchy (4 levels) and verifies that merge correctly handles:
        - Moving subtrees (node with descendants) between parents
        - Creating new nodes at multiple levels
        - Ancestor and descendant relationships across levels
        - MPTT tree levels and structure integrity
        """
        # Create a multi-level hierarchy in main
        # Root (0)
        # ├── Parent A (1)
        # │   ├── Child A1 (2)
        # │   │   └── Grandchild A1-1 (3)
        # │   └── Child A2 (2)
        # └── Parent B (1)
        #     └── Child B1 (2)
        #         └── Grandchild B1-1 (3)

        root_region = Region.objects.create(name='Root', slug='root')

        parent_a = Region.objects.create(name='Parent A', slug='parent-a', parent=root_region)
        parent_a_id = parent_a.id

        child_a1 = Region.objects.create(name='Child A1', slug='child-a1', parent=parent_a)
        child_a1_id = child_a1.id

        grandchild_a1_1 = Region.objects.create(
            name='Grandchild A1-1',
            slug='grandchild-a1-1',
            parent=child_a1
        )
        grandchild_a1_1_id = grandchild_a1_1.id

        child_a2 = Region.objects.create(name='Child A2', slug='child-a2', parent=parent_a)
        child_a2_id = child_a2.id

        parent_b = Region.objects.create(name='Parent B', slug='parent-b', parent=root_region)
        parent_b_id = parent_b.id

        child_b1 = Region.objects.create(name='Child B1', slug='child-b1', parent=parent_b)
        child_b1_id = child_b1.id

        grandchild_b1_1 = Region.objects.create(
            name='Grandchild B1-1',
            slug='grandchild-b1-1',
            parent=child_b1
        )

        # Verify initial hierarchy levels
        self.assertEqual(root_region.level, 0)
        self.assertEqual(parent_a.level, 1)
        self.assertEqual(child_a1.level, 2)
        self.assertEqual(grandchild_a1_1.level, 3)
        self.assertEqual(grandchild_b1_1.level, 3)

        # Verify initial ancestor/descendant relationships
        self.assertEqual(list(grandchild_a1_1.get_ancestors()), [root_region, parent_a, child_a1])
        self.assertEqual(list(child_a1.get_descendants()), [grandchild_a1_1])

        # Create branch
        branch = self._create_and_provision_branch()

        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: restructure the hierarchy
        with activate_branch(branch), event_tracking(request):
            # Move Child A1 (with its grandchild) from Parent A to Parent B
            child_a1 = Region.objects.get(id=child_a1_id)
            child_a1.snapshot()
            child_a1.parent = Region.objects.get(id=parent_b_id)
            child_a1.save()

            # Create a deep nested structure under Child A2 (3 new levels)
            grandchild_a2_1 = Region.objects.create(
                name='Grandchild A2-1',
                slug='grandchild-a2-1',
                parent=Region.objects.get(id=child_a2_id)
            )
            grandchild_a2_1_id = grandchild_a2_1.id

            great_grandchild = Region.objects.create(
                name='Great-Grandchild A2-1-1',
                slug='great-grandchild-a2-1-1',
                parent=grandchild_a2_1
            )
            great_grandchild_id = great_grandchild.id

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Expected hierarchy after merge:
        # Root (0)
        # ├── Parent A (1)
        # │   └── Child A2 (2)
        # │       └── Grandchild A2-1 (3)
        # │           └── Great-Grandchild A2-1-1 (4)
        # └── Parent B (1)
        #     ├── Child A1 (2) ← moved with its descendant
        #     │   └── Grandchild A1-1 (3)
        #     └── Child B1 (2)
        #         └── Grandchild B1-1 (3)

        # Verify Child A1 moved to Parent B (and brought its grandchild)
        child_a1 = Region.objects.get(id=child_a1_id)
        self.assertEqual(child_a1.parent_id, parent_b_id)
        self.assertEqual(child_a1.level, 2)

        # Verify Grandchild A1-1 moved with its parent and has correct ancestor chain
        grandchild_a1_1 = Region.objects.get(id=grandchild_a1_1_id)
        self.assertEqual(grandchild_a1_1.parent_id, child_a1_id)
        self.assertEqual(grandchild_a1_1.level, 3)
        self.assertEqual(list(grandchild_a1_1.get_ancestors()), [
            Region.objects.get(name='Root'),
            Region.objects.get(id=parent_b_id),
            child_a1
        ])

        # Verify Parent B now has two children
        parent_b = Region.objects.get(id=parent_b_id)
        parent_b_children = list(parent_b.get_children())
        self.assertEqual(len(parent_b_children), 2)
        self.assertIn(child_a1, parent_b_children)
        self.assertIn(Region.objects.get(id=child_b1_id), parent_b_children)

        # Verify Parent A now has only Child A2
        parent_a = Region.objects.get(id=parent_a_id)
        self.assertEqual(list(parent_a.get_children()), [Region.objects.get(id=child_a2_id)])

        # Verify new deep hierarchy under Child A2
        grandchild_a2_1 = Region.objects.get(id=grandchild_a2_1_id)
        self.assertEqual(grandchild_a2_1.level, 3)
        self.assertEqual(grandchild_a2_1.parent_id, child_a2_id)

        great_grandchild = Region.objects.get(id=great_grandchild_id)
        self.assertEqual(great_grandchild.level, 4)
        self.assertEqual(great_grandchild.parent_id, grandchild_a2_1_id)
        self.assertEqual(list(great_grandchild.get_ancestors()), [
            Region.objects.get(name='Root'),
            Region.objects.get(id=parent_a_id),
            Region.objects.get(id=child_a2_id),
            grandchild_a2_1
        ])

        # Verify Child A2's descendants
        child_a2 = Region.objects.get(id=child_a2_id)
        child_a2_descendants = list(child_a2.get_descendants())
        self.assertEqual(len(child_a2_descendants), 2)
        self.assertIn(grandchild_a2_1, child_a2_descendants)
        self.assertIn(great_grandchild, child_a2_descendants)

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify original hierarchy is restored
        child_a1 = Region.objects.get(id=child_a1_id)
        self.assertEqual(child_a1.parent_id, parent_a_id)
        self.assertEqual(child_a1.level, 2)

        # Verify Child A1's grandchild is back under original hierarchy
        grandchild_a1_1 = Region.objects.get(id=grandchild_a1_1_id)
        self.assertEqual(list(grandchild_a1_1.get_ancestors()), [
            Region.objects.get(name='Root'),
            Region.objects.get(id=parent_a_id),
            child_a1
        ])

        # Verify Parent A has both children again
        parent_a = Region.objects.get(id=parent_a_id)
        self.assertEqual(len(list(parent_a.get_children())), 2)

        # Verify new nodes are deleted
        self.assertFalse(Region.objects.filter(id=grandchild_a2_1_id).exists())
        self.assertFalse(Region.objects.filter(id=great_grandchild_id).exists())

    def test_merge_many_to_many_tags(self):
        """
        Test adding and removing many-to-many relationships (tags on site).
        Verifies that merge handles M2M changes correctly.
        """
        # Create tags in main
        tag1 = Tag.objects.create(name='Tag 1', slug='tag-1')
        tag2 = Tag.objects.create(name='Tag 2', slug='tag-2')
        tag3 = Tag.objects.create(name='Tag 3', slug='tag-3')

        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        with event_tracking(request):
            site = Site.objects.create(name='Test Site', slug='test-site')
            site.tags.add(tag1, tag2)
        site_id = site.id

        # Verify initial tags
        self.assertEqual(set(site.tags.all()), {tag1, tag2})

        # Create branch
        branch = self._create_and_provision_branch()

        # In branch: modify tags
        with activate_branch(branch), event_tracking(request):
            site = Site.objects.get(id=site_id)
            site.snapshot()
            site.tags.remove(tag2)
            site.tags.add(tag3)
            site.save()

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify tags changed in main
        site = Site.objects.get(id=site_id)
        self.assertEqual(set(site.tags.all()), {tag1, tag3})

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify tags restored to original
        site = Site.objects.get(id=site_id)
        self.assertEqual(set(site.tags.all()), {tag1, tag2})

    def test_merge_m2m_replace_no_false_conflict(self):
        """
        Test that replacing M2M values (removing one, adding another) in a branch does not
        produce a false conflict in ChangeDiff. The bug caused 'current' M2M data to be
        serialized from the branch schema rather than main, yielding an incorrect conflict.
        Refs: #298
        """
        tag1 = Tag.objects.create(name='Tag A', slug='tag-a')
        tag2 = Tag.objects.create(name='Tag B', slug='tag-b')
        tag3 = Tag.objects.create(name='Tag C', slug='tag-c')

        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        with event_tracking(request):
            site = Site.objects.create(name='Test Site', slug='test-site')
            site.tags.add(tag1, tag2)
        site_id = site.id

        # Create branch
        branch = self._create_and_provision_branch()

        # In branch: replace tag2 with tag3
        with activate_branch(branch), event_tracking(request):
            site = Site.objects.get(id=site_id)
            site.snapshot()
            site.tags.remove(tag2)
            site.tags.add(tag3)
            site.save()

        # Verify no false conflict is recorded — 'current' must reflect main, not branch
        content_type = ContentType.objects.get_for_model(Site)
        diff = ChangeDiff.objects.get(branch=branch, object_type=content_type, object_id=site_id)
        self.assertIsNone(diff.conflicts, f'False conflict detected for M2M replacement: {diff.conflicts}')

        # Merge should succeed
        branch.merge(user=self.user, commit=True)

        site = Site.objects.get(id=site_id)
        self.assertEqual(set(site.tags.all()), {tag1, tag3})

    def test_merge_virtual_chassis_dissociation(self):
        """
        Test dissociating all members from a virtual chassis in a branch.

        The required ordering is: remove non-master members first, then clear the master
        designation on the VC, then remove the former master device. Applying these changes
        out of order triggers a ValidationError because Device.clean() prevents removing a
        device from a VC while it is still designated as its master.

        VirtualChassis is the primary DCIM model with this ordering constraint. Other
        member-style relationships (Interface LAG, DeviceBay) use SET_NULL without an
        equivalent hard validation, so no additional models require this test pattern.
        Refs: #293, #349
        """
        site = Site.objects.create(name='Test Site', slug='test-site')
        vc = VirtualChassis.objects.create(name='Test VC')
        device1 = Device.objects.create(
            name='VC Master',
            site=site,
            device_type=self.device_type,
            role=self.device_role,
            virtual_chassis=vc,
            vc_position=1,
        )
        device2 = Device.objects.create(
            name='VC Member',
            site=site,
            device_type=self.device_type,
            role=self.device_role,
            virtual_chassis=vc,
            vc_position=2,
        )
        vc.master = device1
        vc.save()
        vc_id = vc.id
        device1_id = device1.id
        device2_id = device2.id

        # Create branch
        branch = self._create_and_provision_branch()

        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: dissociate all devices in safe order
        with activate_branch(branch), event_tracking(request):
            # Step 1: remove the non-master member
            device2_branch = Device.objects.get(id=device2_id)
            device2_branch.snapshot()
            device2_branch.virtual_chassis = None
            device2_branch.vc_position = None
            device2_branch.save()

            # Step 2: clear master designation so device1 can be safely removed
            vc_branch = VirtualChassis.objects.get(id=vc_id)
            vc_branch.snapshot()
            vc_branch.master = None
            vc_branch.save()

            # Step 3: remove the former master (now safe — VC has no master)
            device1_branch = Device.objects.get(id=device1_id)
            device1_branch.snapshot()
            device1_branch.virtual_chassis = None
            device1_branch.vc_position = None
            device1_branch.save()

        # Merge should succeed without ValidationError
        branch.merge(user=self.user, commit=True)

        vc.refresh_from_db()
        device1.refresh_from_db()
        device2.refresh_from_db()
        self.assertIsNone(vc.master)
        self.assertIsNone(device1.virtual_chassis)
        self.assertIsNone(device2.virtual_chassis)

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify original VC assignments are restored
        vc.refresh_from_db()
        device1.refresh_from_db()
        device2.refresh_from_db()
        self.assertEqual(device1.virtual_chassis_id, vc_id)
        self.assertEqual(device2.virtual_chassis_id, vc_id)
        self.assertEqual(vc.master_id, device1_id)

    def test_merge_cable_path_recalculation(self):
        """
        Test that cable paths are recalculated after merging a branch containing a new cable.

        The bug was that _terminations_modified was not set on the Cable instance during
        merge (the cable was deserialized from an ObjectChange rather than created
        programmatically), so update_connected_endpoints() was never triggered and
        CablePath objects were not created, leaving interfaces without end-to-end paths.
        Refs: #150
        """
        site = Site.objects.create(name='Test Site', slug='test-site')
        device_a = Device.objects.create(
            name='Device A',
            site=site,
            device_type=self.device_type,
            role=self.device_role,
        )
        device_b = Device.objects.create(
            name='Device B',
            site=site,
            device_type=self.device_type,
            role=self.device_role,
        )
        interface_a = Interface.objects.create(device=device_a, name='eth0', type='1000base-t')
        interface_b = Interface.objects.create(device=device_b, name='eth0', type='1000base-t')
        interface_a_id = interface_a.id
        interface_b_id = interface_b.id

        # Create branch
        branch = self._create_and_provision_branch()

        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: connect the two interfaces with a cable
        with activate_branch(branch), event_tracking(request):
            cable = Cable(
                a_terminations=[Interface.objects.get(id=interface_a_id)],
                b_terminations=[Interface.objects.get(id=interface_b_id)],
            )
            cable.save()
            cable_id = cable.id

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify cable exists in main
        self.assertTrue(Cable.objects.filter(id=cable_id).exists())

        # Verify cable paths were recalculated — not left empty after merge (#150 regression)
        # A successful cable connection creates two CablePath records (one per endpoint)
        self.assertEqual(CablePath.objects.count(), 2, 'Cable paths not populated after merge')

        # Revert branch
        branch.revert(user=self.user, commit=True)

        # Verify cable and its paths are removed after revert
        self.assertFalse(Cable.objects.filter(id=cable_id).exists())
        self.assertEqual(CablePath.objects.count(), 0)

    def test_merge_edit_then_delete_after_main_delete(self):
        """
        This tests that deleting an object in a branch that was deleted in main
        works correctly even when there are prior edits to that object in the branch.
        """
        # Create site in main
        site = Site.objects.create(name='Site 1', slug='site-1', description='Original description')
        site_id = site.id

        # Create and activate branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # Edit site in branch
        with activate_branch(branch), event_tracking(request):
            site_in_branch = Site.objects.get(id=site_id)
            site_in_branch.snapshot()
            site_in_branch.description = 'Updated in branch'
            site_in_branch.save()

        # Verify the update was recorded
        self._assert_object_changes(branch, Site, site_id, 1, ['update'])

        # Go back to main and delete site
        site.delete()
        self.assertFalse(Site.objects.filter(id=site_id).exists())

        # Activate branch and delete site
        with activate_branch(branch), event_tracking(request):
            site_in_branch = Site.objects.get(id=site_id)
            site_in_branch.delete()

        # Verify both update and delete were recorded
        self._assert_object_changes(branch, Site, site_id, 2, ['update', 'delete'])

        # Merge branch - should succeed
        branch.merge(user=self.user, commit=True)

        # Verify branch status
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)


class IterativeMergeTestCase(BaseMergeTests, TransactionTestCase):
    """Test cases for Branch merge using iterative merge strategy."""

    def _create_and_provision_branch(self, name='Test Branch'):
        """Helper to create and provision a branch with iterative merge strategy."""
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
