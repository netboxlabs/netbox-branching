"""
Tests for Branch merge functionality with ObjectChange collapsing.
"""
import uuid

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from netbox.context_managers import event_tracking
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models import Branch
from netbox_branching.utilities import activate_branch


User = get_user_model()


class MergeTestCase(TransactionTestCase):
    """Test cases for Branch merge with ObjectChange collapsing and ordering."""

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
        branch = Branch(name=name)
        branch.save(provision=False)
        branch.provision(user=self.user)
        branch.refresh_from_db()  # Refresh to get updated status
        return branch

    def test_merge_basic_create(self):
        """
        Test basic create operation.
        Verifies object is created in main and ObjectChange was tracked.
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

        # Verify site exists in main
        self.assertTrue(Site.objects.filter(id=site_id).exists())
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.name, 'Test Site')
        self.assertEqual(site.slug, 'test-site')

    def test_merge_basic_update(self):
        """
        Test basic update operation.
        Verifies object is updated in main and ObjectChange was tracked.
        """
        # Create site in main
        site = Site.objects.create(name='Original Site', slug='test-site', description='Original')
        site_id = site.id

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        # In branch: update site
        with activate_branch(branch), event_tracking(request):
            site = Site.objects.get(id=site_id)
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

        # Verify site is updated in main
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.description, 'Updated')

    def test_merge_basic_delete(self):
        """
        Test basic delete operation.
        Verifies object is deleted in main and ObjectChange was tracked.
        """
        # Create site in main
        site = Site.objects.create(name='Test Site', slug='test-site')
        site_id = site.id

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

    def test_merge_basic_create_update_delete(self):
        """
        Test create, update, then delete same object.
        Verifies object is skipped (not in main) after collapsing.
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

    def test_merge_delete_then_create_same_slug(self):
        """
        Test delete object, then create object with same unique constraint value (slug).
        Verifies deletes free up unique constraints before creates.
        """
        # Create site in main
        site1 = Site.objects.create(name='Site 1', slug='site-1')
        site1_id = site1.id

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

    def test_merge_create_device_and_delete_old(self):
        """
        Test create new object, then delete old object.
        Verifies proper ordering with cascade delete dependencies.
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

        interface_a = Interface.objects.create(
            device=device_a,
            name='eth0',
            type='1000base-t'
        )
        interface_a_id = interface_a.id

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

    def test_merge_slug_rename_then_create(self):
        """
        Test update object's unique field, then create new object with old value.
        Verifies updates free up unique constraints before creates.
        """
        # Create site in main
        site1 = Site.objects.create(name='Site 1', slug='site-1')
        site1_id = site1.id

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()  # Set request id for ObjectChange tracking
        request.user = self.user

        # In branch: rename site1 slug, create new site with old slug
        with activate_branch(branch), event_tracking(request):
            site1 = Site.objects.get(id=site1_id)
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

    def test_merge_multiple_updates_collapsed(self):
        """
        Test multiple updates to same object.
        Verifies consecutive non-referencing updates are collapsed.
        """
        # Create site in main
        site = Site.objects.create(name='Site 1', slug='site-1', description='Original')
        site_id = site.id

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()  # Set request id for ObjectChange tracking
        request.user = self.user

        # In branch: update site multiple times
        with activate_branch(branch), event_tracking(request):
            site = Site.objects.get(id=site_id)

            site.description = 'Update 1'
            site.save()

            site.description = 'Update 2'
            site.save()

            site.name = 'Site 1 Modified'
            site.save()

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify main schema - should have final state
        site = Site.objects.get(id=site_id)
        self.assertEqual(site.name, 'Site 1 Modified')
        self.assertEqual(site.description, 'Update 2')

    def test_merge_create_with_multiple_updates(self):
        """
        Test create object then update it multiple times.
        Verifies create is kept separate from updates.
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

            site.description = 'Modified 1'
            site.save()

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

    def test_merge_complex_dependency_chain(self):
        """
        Test complex scenario with multiple creates, updates, and deletes.
        Verifies correct ordering with FK dependencies and references.
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
            interface_b = Interface.objects.create(
                device=device_b,
                name='eth0',
                type='1000base-t'
            )
            interface_b_id = interface_b.id

            # Create another interface on device_b
            interface_c = Interface.objects.create(
                device=device_b,
                name='eth1',
                type='1000base-t'
            )

            # Update device_b
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

    def test_merge_conflicting_slug_create_update_delete(self):
        """
        Test create object with conflicting unique constraint, update it, then delete it.
        Verifies skipped object doesn't cause constraint violations.
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

    def test_merge_slug_update_causes_then_resolves_conflict(self):
        """
        Test create object, update to conflicting unique constraint, then update to resolve.
        Verifies final non-conflicting state merges successfully.
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
            site_branch.slug = 'conflict-slug'
            site_branch.save()

            # Update again to resolve conflict
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
