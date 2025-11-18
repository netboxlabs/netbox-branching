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

    def test_merge_delete_then_create_same_slug(self):
        """
        Test merging when a site is deleted and a new site with the same slug is created.
        This was the original bug: deletes must happen before creates to free up unique constraints.
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
        Test merging when a new device is created and an old device is deleted.
        Tests ordering with dependencies.
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

    def test_merge_create_and_delete_same_object(self):
        """
        Test that creating and deleting the same object in a branch results in no change (skip).
        """
        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()  # Set request id for ObjectChange tracking
        request.user = self.user

        # In branch: create and delete a site
        with activate_branch(branch), event_tracking(request):
            site = Site.objects.create(name='Temp Site', slug='temp-site')
            site_id = site.id
            site.delete()

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify main schema - site should not exist
        self.assertFalse(Site.objects.filter(id=site_id).exists())

        # Verify merge succeeded
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)

    def test_merge_slug_rename_then_create(self):
        """
        Test merging when a site's slug is changed, then a new site is created with the old slug.
        Updates should happen before creates to free up the slug.
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
        Test that multiple updates to the same object are collapsed into a single update.
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
        Test that creating an object and then updating it multiple times
        results in a single create with the final state.
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
        Test a complex scenario with creates, updates, and deletes with dependencies.
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

    def test_merge_delete_ordering_by_time(self):
        """
        Test that deletes maintain time order when there are no dependencies.
        """
        # Create sites in main
        site1 = Site.objects.create(name='Site 1', slug='site-1')
        site2 = Site.objects.create(name='Site 2', slug='site-2')
        site3 = Site.objects.create(name='Site 3', slug='site-3')

        site1_id = site1.id
        site2_id = site2.id
        site3_id = site3.id

        # Create branch
        branch = self._create_and_provision_branch()

        # Create a request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()  # Set request id for ObjectChange tracking
        request.user = self.user

        # In branch: delete sites in specific order
        with activate_branch(branch), event_tracking(request):
            Site.objects.get(id=site1_id).delete()
            Site.objects.get(id=site3_id).delete()
            Site.objects.get(id=site2_id).delete()

        # Merge branch
        branch.merge(user=self.user, commit=True)

        # Verify all deleted
        self.assertEqual(Site.objects.count(), 0)

        # Verify merge succeeded
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)
