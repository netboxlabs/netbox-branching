"""
Tests for Branch sync functionality.

Sync takes changes from the main schema and applies them to the branch schema,
keeping the branch up-to-date with changes made in main since the branch was
provisioned (or last synced). This is the opposite of merge, which takes branch
changes and applies them to main.

Unlike merge, there are no different strategies for sync — changes are always
applied iteratively in chronological order.
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


class SyncTestCase(TransactionTestCase):
    """
    Test cases for Branch sync functionality.

    Sync applies changes from the main schema to the branch schema. Only
    ObjectChange records created in main AFTER the branch was provisioned
    (or after the last sync) are applied.
    """

    serialized_rollback = True

    def setUp(self):
        """Set up common test data."""
        self.user = User.objects.create_user(username='testuser')

        # Create a shared request context for event tracking
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user
        self.request = request

        # Create base objects in main needed for device-related tests
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
        """Helper to create and provision a branch, waiting until READY."""
        branch = Branch(name=name, merge_strategy='squash')
        branch.save(provision=False)
        branch.provision(user=self.user)

        max_wait = 30
        wait_interval = 0.1
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

    # -------------------------------------------------------------------------
    # No-op scenario
    # -------------------------------------------------------------------------

    def test_sync_no_changes(self):
        """
        Test sync when there are no new changes in main to apply.
        Sync: exits early without modifying the branch; last_sync is not updated.
        """
        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # No changes made in main after provisioning
        self.assertEqual(branch.get_unsynced_changes().count(), 0)

        # Sync (should be a no-op)
        branch.sync(user=self.user, commit=True)

        # Branch still READY and last_sync unchanged (sync returned early)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)
        self.assertEqual(branch.last_sync, initial_last_sync)

    # -------------------------------------------------------------------------
    # CRUD scenarios
    # -------------------------------------------------------------------------

    def test_sync_crud_in_main(self):
        """
        Test that sync applies create, update, and delete changes from main
        to the branch in chronological order.
        """
        # Create some sites in main before branch provisioning
        with event_tracking(self.request):
            site_to_update = Site.objects.create(
                name='Update Me', slug='update-me', description='Original'
            )
            site_to_delete = Site.objects.create(name='Delete Me', slug='delete-me')
        update_id = site_to_update.id
        delete_id = site_to_delete.id

        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # Make multiple changes in main AFTER branch provisioning
        with event_tracking(self.request):
            # Create a new site
            new_site = Site.objects.create(name='Brand New', slug='brand-new')
            new_id = new_site.id

            # Update an existing site
            site_to_update = Site.objects.get(id=update_id)
            site_to_update.snapshot()
            site_to_update.description = 'Updated in main'
            site_to_update.save()

            # Delete the other existing site
            Site.objects.get(id=delete_id).delete()

        # Sync branch
        branch.sync(user=self.user, commit=True)

        # Verify all three changes applied to branch
        with activate_branch(branch):
            # New site now exists in branch
            self.assertTrue(Site.objects.filter(id=new_id).exists())
            self.assertEqual(Site.objects.get(id=new_id).name, 'Brand New')

            # Updated site reflects main's change
            self.assertEqual(Site.objects.get(id=update_id).description, 'Updated in main')

            # Deleted site is gone from branch
            self.assertFalse(Site.objects.filter(id=delete_id).exists())

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    # -------------------------------------------------------------------------
    # Conflict / concurrent modification scenarios
    # -------------------------------------------------------------------------

    def test_sync_delete_in_main_while_updated_in_branch(self):
        """
        Test sync when an object was updated in branch, but then deleted in main.
        Sync: applies the deletion from main, removing the object from branch even
        though branch had modified it. Main's deletion takes precedence.
        """
        # Create site in main before branch provisioning
        with event_tracking(self.request):
            site = Site.objects.create(
                name='Contested Site', slug='contested-site', description='Original'
            )
            site_id = site.id

        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # In branch: update the site
        with activate_branch(branch), event_tracking(self.request):
            branch_site = Site.objects.get(id=site_id)
            branch_site.snapshot()
            branch_site.description = 'Updated in branch'
            branch_site.save()

        # Verify branch has the updated site
        with activate_branch(branch):
            self.assertEqual(Site.objects.get(id=site_id).description, 'Updated in branch')

        # In main: delete the site
        with event_tracking(self.request):
            Site.objects.get(id=site_id).delete()

        # Sync branch: applies the deletion from main
        branch.sync(user=self.user, commit=True)

        # Site is deleted from branch (main's delete wins on sync)
        with activate_branch(branch):
            self.assertFalse(Site.objects.filter(id=site_id).exists())

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    def test_sync_m2m_tags_concurrent_changes(self):
        """
        Test sync with concurrent many-to-many (tag) changes in both main and branch.
        Sync applies main's ObjectChange to the branch, so the branch ends up with
        main's post-change tag state regardless of what the branch changed.

        Scenario:
          1. Create site in main with tags {tag1, tag2}
          2. Create branch (branch inherits {tag1, tag2})
          3. In branch: change tags to {tag1, tag3} (remove tag2, add tag3)
          4. In main:   change tags to {tag2, tag4} (remove tag1, add tag4)
          5. Sync: main's tag update is applied to branch
          6. Branch ends up with {tag2, tag4} (main's post-change state)
        """
        # Create tags in main
        tag1 = Tag.objects.create(name='Tag 1', slug='tag-1')
        tag2 = Tag.objects.create(name='Tag 2', slug='tag-2')
        tag3 = Tag.objects.create(name='Tag 3', slug='tag-3')
        tag4 = Tag.objects.create(name='Tag 4', slug='tag-4')

        # Create site with initial tags {tag1, tag2}
        with event_tracking(self.request):
            site = Site.objects.create(name='Tagged Site', slug='tagged-site')
            site.tags.add(tag1, tag2)
        site_id = site.id

        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # Verify branch inherited the initial tags
        with activate_branch(branch):
            self.assertEqual(set(Site.objects.get(id=site_id).tags.all()), {tag1, tag2})

        # In branch: remove tag2, add tag3 → branch has {tag1, tag3}
        with activate_branch(branch), event_tracking(self.request):
            branch_site = Site.objects.get(id=site_id)
            branch_site.snapshot()
            branch_site.tags.remove(tag2)
            branch_site.tags.add(tag3)
            branch_site.save()

        with activate_branch(branch):
            self.assertEqual(set(Site.objects.get(id=site_id).tags.all()), {tag1, tag3})

        # In main: remove tag1, add tag4 → main has {tag2, tag4}
        with event_tracking(self.request):
            main_site = Site.objects.get(id=site_id)
            main_site.snapshot()
            main_site.tags.remove(tag1)
            main_site.tags.add(tag4)
            main_site.save()

        self.assertEqual(set(Site.objects.get(id=site_id).tags.all()), {tag2, tag4})

        # Sync branch: applies main's tag change to branch schema
        branch.sync(user=self.user, commit=True)

        # Branch should now have main's post-change tags: {tag2, tag4}
        with activate_branch(branch):
            self.assertEqual(set(Site.objects.get(id=site_id).tags.all()), {tag2, tag4})

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    # -------------------------------------------------------------------------
    # FK reference / cascade scenarios
    # -------------------------------------------------------------------------

    def test_sync_delete_in_main_with_branch_fk_reference(self):
        """
        Test sync when a region is deleted in main while both:
          - The branch has modified that region
          - The branch has created a site that references that region

        Scenario:
          1. Create Region R in main
          2. Create branch
          3. In branch: update Region R, and create Site S with region=R
          4. In main: delete Region R
          5. Sync: Region R deletion applied to branch.
                   Site S's region field is SET to NULL (on_delete=SET_NULL cascade).
        """
        # Create region in main before branch provisioning
        with event_tracking(self.request):
            region = Region.objects.create(name='Region R', slug='region-r')
            region_id = region.id

        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # In branch: update Region R and create Site S referencing it
        with activate_branch(branch), event_tracking(self.request):
            branch_region = Region.objects.get(id=region_id)
            branch_region.snapshot()
            branch_region.description = 'Updated in branch'
            branch_region.save()

            branch_site = Site.objects.create(
                name='Branch Site',
                slug='branch-site',
                region=Region.objects.get(id=region_id)
            )
            branch_site_id = branch_site.id

        # Verify the branch state before sync
        with activate_branch(branch):
            self.assertTrue(Region.objects.filter(id=region_id).exists())
            self.assertEqual(Site.objects.get(id=branch_site_id).region_id, region_id)

        # In main: delete Region R
        with event_tracking(self.request):
            Region.objects.get(id=region_id).delete()

        self.assertFalse(Region.objects.filter(id=region_id).exists())

        # Sync branch: applies Region R deletion to branch schema
        branch.sync(user=self.user, commit=True)

        # Region R is gone from branch; Site S still exists but region is NULL (SET_NULL)
        with activate_branch(branch):
            self.assertFalse(Region.objects.filter(id=region_id).exists())
            self.assertTrue(Site.objects.filter(id=branch_site_id).exists())
            synced_site = Site.objects.get(id=branch_site_id)
            self.assertIsNone(synced_site.region_id)

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    def test_sync_create_with_fk_dependency_in_main(self):
        """
        Test sync when a device and its interface are created in main after the
        branch was provisioned. Both the device and interface should appear in
        the branch after sync.
        """
        # Create site in main before branch provisioning
        with event_tracking(self.request):
            site = Site.objects.create(name='Device Site', slug='device-site')
            site_id = site.id

        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # Create device and interface in main AFTER branch provisioning
        with event_tracking(self.request):
            device = Device.objects.create(
                name='Main Device',
                site=Site.objects.get(id=site_id),
                device_type=self.device_type,
                role=self.device_role
            )
            device_id = device.id

            interface = Interface.objects.create(
                device=device,
                name='eth0',
                type='1000base-t'
            )
            interface_id = interface.id

        # Sync branch
        branch.sync(user=self.user, commit=True)

        # Both device and interface should now exist in branch
        with activate_branch(branch):
            self.assertTrue(Device.objects.filter(id=device_id).exists())
            self.assertTrue(Interface.objects.filter(id=interface_id).exists())
            synced_device = Device.objects.get(id=device_id)
            self.assertEqual(synced_device.name, 'Main Device')
            synced_interface = Interface.objects.get(id=interface_id)
            self.assertEqual(synced_interface.device_id, device_id)

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    # -------------------------------------------------------------------------
    # MPTT tree scenarios
    # -------------------------------------------------------------------------

    def test_sync_mptt_create_in_main(self):
        """
        Test sync when a new MPTT node is added in main after branch provisioning.
        Sync: the new node appears in the branch with correct hierarchy.
        """
        # Create root region in main before branch provisioning
        with event_tracking(self.request):
            root = Region.objects.create(name='Root Region', slug='root-region')
            root_id = root.id

        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # Verify root exists in branch but no child yet
        with activate_branch(branch):
            self.assertTrue(Region.objects.filter(id=root_id).exists())
            self.assertEqual(Region.objects.get(id=root_id).get_children().count(), 0)

        # Add a child region in main AFTER branch provisioning
        with event_tracking(self.request):
            child = Region.objects.create(
                name='Child Region', slug='child-region', parent=Region.objects.get(id=root_id)
            )
            child_id = child.id

        # Sync branch
        branch.sync(user=self.user, commit=True)

        # Child region should now exist in branch under root
        with activate_branch(branch):
            self.assertTrue(Region.objects.filter(id=child_id).exists())
            branch_child = Region.objects.get(id=child_id)
            self.assertEqual(branch_child.parent_id, root_id)

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    def test_sync_mptt_delete_in_main_with_branch_extension(self):
        """
        Test sync with MPTT tree where main deletes an ancestor while the branch
        has extended the tree with an additional child node.

        Scenario:
          1. Create 2-level Region tree in main: Root → Child
          2. Create branch
          3. In branch: add Grandchild (Root → Child → Grandchild)
          4. In main: delete Root (CASCADE to Child via MPTT parent FK)
          5. Sync: Root and Child deletions are applied to branch. The cascade
                   from deleting Child also removes Grandchild from the branch.
        """
        # Create 2-level region hierarchy in main before branch provisioning
        with event_tracking(self.request):
            root = Region.objects.create(name='Root Region', slug='root-region')
            child = Region.objects.create(name='Child Region', slug='child-region', parent=root)
        root_id = root.id
        child_id = child.id

        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # In branch: add a grandchild (third level)
        with activate_branch(branch), event_tracking(self.request):
            grandchild = Region.objects.create(
                name='Grandchild Region',
                slug='grandchild-region',
                parent=Region.objects.get(id=child_id)
            )
            grandchild_id = grandchild.id

        # Verify the full 3-level hierarchy exists in branch
        with activate_branch(branch):
            self.assertTrue(Region.objects.filter(id=root_id).exists())
            self.assertTrue(Region.objects.filter(id=child_id).exists())
            self.assertTrue(Region.objects.filter(id=grandchild_id).exists())
            gc = Region.objects.get(id=grandchild_id)
            self.assertEqual(gc.parent_id, child_id)

        # In main: delete Root (CASCADE deletes Child via MPTT parent FK)
        with event_tracking(self.request):
            Region.objects.get(id=root_id).delete()

        # Root and Child are gone from main
        self.assertFalse(Region.objects.filter(id=root_id).exists())
        self.assertFalse(Region.objects.filter(id=child_id).exists())

        # Sync branch: applies Root and Child deletions from main
        branch.sync(user=self.user, commit=True)

        # After sync: Root and Child gone from branch, and Grandchild cascade-deleted
        # because its parent (Child) was deleted from the branch schema
        with activate_branch(branch):
            self.assertFalse(Region.objects.filter(id=root_id).exists())
            self.assertFalse(Region.objects.filter(id=child_id).exists())
            self.assertFalse(Region.objects.filter(id=grandchild_id).exists())

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    def test_sync_mptt_update_in_main(self):
        """
        Test sync with an MPTT node that is updated in main (e.g. reparented).
        Sync: the node's updated parent relationship appears in branch.
        """
        # Create a 2-level tree and a sibling root in main before branch provisioning
        with event_tracking(self.request):
            root_a = Region.objects.create(name='Root A', slug='root-a')
            root_b = Region.objects.create(name='Root B', slug='root-b')
            child = Region.objects.create(name='Child', slug='child', parent=root_a)
        root_a_id = root_a.id
        root_b_id = root_b.id
        child_id = child.id

        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # Verify child is under root_a in branch
        with activate_branch(branch):
            self.assertEqual(Region.objects.get(id=child_id).parent_id, root_a_id)

        # In main: reparent child to root_b
        with event_tracking(self.request):
            child = Region.objects.get(id=child_id)
            child.snapshot()
            child.parent = Region.objects.get(id=root_b_id)
            child.save()

        # Sync branch
        branch.sync(user=self.user, commit=True)

        # Child should now be under root_b in branch
        with activate_branch(branch):
            branch_child = Region.objects.get(id=child_id)
            self.assertEqual(branch_child.parent_id, root_b_id)

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    def test_sync_mptt_branch_and_main_extend_tree(self):
        """
        Test sync when both branch and main independently add children to the
        same parent. After sync, the branch should contain children from both
        sources.

        Scenario:
          1. Create root Region in main
          2. Create branch
          3. In branch: add Child-Branch under root
          4. In main: add Child-Main under root
          5. Sync: Child-Main is synced into branch; branch retains Child-Branch
        """
        # Create root in main before branch provisioning
        with event_tracking(self.request):
            root = Region.objects.create(name='Root', slug='root')
            root_id = root.id

        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # In branch: add a child
        with activate_branch(branch), event_tracking(self.request):
            child_branch = Region.objects.create(
                name='Child Branch', slug='child-branch', parent=Region.objects.get(id=root_id)
            )
            child_branch_id = child_branch.id

        # In main: add a different child
        with event_tracking(self.request):
            child_main = Region.objects.create(
                name='Child Main', slug='child-main', parent=Region.objects.get(id=root_id)
            )
            child_main_id = child_main.id

        # Sync branch
        branch.sync(user=self.user, commit=True)

        # Branch should now have both children: one from branch, one from main
        with activate_branch(branch):
            self.assertTrue(Region.objects.filter(id=child_branch_id).exists())
            self.assertTrue(Region.objects.filter(id=child_main_id).exists())

            root_in_branch = Region.objects.get(id=root_id)
            children = list(root_in_branch.get_children())
            self.assertEqual(len(children), 2)

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    # -------------------------------------------------------------------------
    # M2M false conflict detection
    # -------------------------------------------------------------------------

    def test_sync_m2m_no_false_conflict(self):
        """
        Test that making M2M changes in a branch does not produce a false conflict
        in ChangeDiff, and that syncing subsequent main M2M changes is applied
        correctly to the branch.

        The bug (#298) caused ChangeDiff.current to be serialized from the branch
        schema rather than main, yielding an incorrect conflict record. Sync then
        applies main's change, verifying the full flow works end-to-end.
        Refs: #298
        """
        tag1 = Tag.objects.create(name='Tag A', slug='tag-a')
        tag2 = Tag.objects.create(name='Tag B', slug='tag-b')
        tag3 = Tag.objects.create(name='Tag C', slug='tag-c')
        tag4 = Tag.objects.create(name='Tag D', slug='tag-d')

        # Create site with tag1+tag2 in main
        with event_tracking(self.request):
            site = Site.objects.create(name='Tagged Site', slug='tagged-site')
            site.tags.add(tag1, tag2)
        site_id = site.id

        # Create branch
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # In branch: replace tag2 with tag3 (site now has {tag1, tag3})
        with activate_branch(branch), event_tracking(self.request):
            site = Site.objects.get(id=site_id)
            site.snapshot()
            site.tags.remove(tag2)
            site.tags.add(tag3)
            site.save()

        # Verify no false conflict — ChangeDiff.current must reflect main ({tag1, tag2}),
        # not the branch schema ({tag1, tag3}). This is the #298 regression check.
        content_type = ContentType.objects.get_for_model(Site)
        diff = ChangeDiff.objects.get(branch=branch, object_type=content_type, object_id=site_id)
        self.assertIsNone(diff.conflicts, f'False conflict detected before sync: {diff.conflicts}')

        # In main: add tag4 (tags become {tag1, tag2, tag4})
        with event_tracking(self.request):
            main_site = Site.objects.get(id=site_id)
            main_site.snapshot()
            main_site.tags.add(tag4)
            main_site.save()

        # Sync: applies main's tag update to branch schema
        branch.sync(user=self.user, commit=True)

        # After sync: branch has main's post-change tag state
        with activate_branch(branch):
            branch_tags = set(Site.objects.get(id=site_id).tags.all())
            self.assertIn(tag4, branch_tags)

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    # -------------------------------------------------------------------------
    # Virtual chassis scenarios
    # -------------------------------------------------------------------------

    def test_sync_virtual_chassis_dissociation(self):
        """
        Test sync when a virtual chassis is fully dissociated in main.

        The safe dissociation order (remove non-master member → clear master
        designation → remove former master) must be respected when applying those
        changes to the branch via sync.
        Refs: #293, #349
        """
        # Create VC with master and member in main before branch provisioning
        with event_tracking(self.request):
            site = Site.objects.create(name='VC Site', slug='vc-site')
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

        # Create branch (inherits VC state from main)
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # Verify branch inherited the VC assignments
        with activate_branch(branch):
            self.assertEqual(Device.objects.get(id=device1_id).virtual_chassis_id, vc_id)
            self.assertEqual(Device.objects.get(id=device2_id).virtual_chassis_id, vc_id)
            self.assertEqual(VirtualChassis.objects.get(id=vc_id).master_id, device1_id)

        # In main: dissociate all devices in safe order
        with event_tracking(self.request):
            # Step 1: remove the non-master member
            device2_obj = Device.objects.get(id=device2_id)
            device2_obj.snapshot()
            device2_obj.virtual_chassis = None
            device2_obj.vc_position = None
            device2_obj.save()

            # Step 2: clear master designation so device1 can be safely removed
            vc_obj = VirtualChassis.objects.get(id=vc_id)
            vc_obj.snapshot()
            vc_obj.master = None
            vc_obj.save()

            # Step 3: remove the former master (safe — VC has no master)
            device1_obj = Device.objects.get(id=device1_id)
            device1_obj.snapshot()
            device1_obj.virtual_chassis = None
            device1_obj.vc_position = None
            device1_obj.save()

        # Verify main state after dissociation
        self.assertIsNone(Device.objects.get(id=device1_id).virtual_chassis_id)
        self.assertIsNone(Device.objects.get(id=device2_id).virtual_chassis_id)
        self.assertIsNone(VirtualChassis.objects.get(id=vc_id).master_id)

        # Sync: applies all VC dissociation changes from main to branch
        branch.sync(user=self.user, commit=True)

        # After sync: branch should reflect main's dissociated state
        with activate_branch(branch):
            self.assertIsNone(Device.objects.get(id=device1_id).virtual_chassis_id)
            self.assertIsNone(Device.objects.get(id=device2_id).virtual_chassis_id)
            self.assertIsNone(VirtualChassis.objects.get(id=vc_id).master_id)

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    # -------------------------------------------------------------------------
    # Cable path scenarios
    # -------------------------------------------------------------------------

    def test_sync_cable_path_recalculation(self):
        """
        Test that cable paths are populated in the branch after syncing a cable
        that was created in main.

        Sync applies changes iteratively, so the Cable CREATE → CableTermination
        CREATEs → Cable UPDATE sequence from main is replayed in the branch,
        allowing trace_paths to fire and create the CablePath records.
        Refs: #150
        """
        # Create devices and interfaces in main before branch provisioning
        with event_tracking(self.request):
            site = Site.objects.create(name='Cable Site', slug='cable-site')
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

        # Create branch (no cable yet)
        branch = self._create_and_provision_branch()
        initial_last_sync = branch.last_sync

        # Verify no cable paths in branch before sync
        with activate_branch(branch):
            self.assertEqual(CablePath.objects.count(), 0)

        # In main: create a cable connecting the two interfaces
        with event_tracking(self.request):
            cable = Cable(
                a_terminations=[Interface.objects.get(id=interface_a_id)],
                b_terminations=[Interface.objects.get(id=interface_b_id)],
            )
            cable.save()
            cable_id = cable.id

        # Verify cable paths exist in main (sanity check)
        self.assertEqual(CablePath.objects.count(), 2)

        # Sync: applies cable creation (and path recalculation) to branch
        branch.sync(user=self.user, commit=True)

        # After sync: cable and cable paths should exist in branch
        with activate_branch(branch):
            self.assertTrue(Cable.objects.filter(id=cable_id).exists())
            self.assertEqual(
                CablePath.objects.count(), 2,
                'Cable paths not populated in branch after sync (#150 regression)'
            )

        branch.refresh_from_db()
        self.assertGreater(branch.last_sync, initial_last_sync)

    # -------------------------------------------------------------------------
    # Double-delete scenario
    # -------------------------------------------------------------------------

    def test_sync_delete_already_deleted_in_branch(self):
        """
        Test sync when an object was deleted in the branch AND also deleted in main.

        The branch's own DELETE ChangeDiff causes get_unsynced_changes() to exclude
        main's DELETE for the same object, so sync is a no-op for that object. The
        branch must remain READY with the object still absent from its schema.
        Refs: #422
        """
        # Create site in main before branch provisioning
        with event_tracking(self.request):
            site = Site.objects.create(name='Doomed Site', slug='doomed-site')
        site_id = site.id

        # Create branch (inherits the site)
        branch = self._create_and_provision_branch()

        # In branch: delete the site
        with activate_branch(branch), event_tracking(self.request):
            Site.objects.get(id=site_id).delete()

        # Verify site is gone from branch schema
        with activate_branch(branch):
            self.assertFalse(Site.objects.filter(id=site_id).exists())

        # Site still exists in main at this point
        self.assertTrue(Site.objects.filter(id=site_id).exists())

        # In main: also delete the site
        with event_tracking(self.request):
            Site.objects.get(id=site_id).delete()
        self.assertFalse(Site.objects.filter(id=site_id).exists())

        # Sync: no errors thrown; branch remains READY
        branch.sync(user=self.user, commit=True)

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)

        # Object remains gone from branch schema
        with activate_branch(branch):
            self.assertFalse(Site.objects.filter(id=site_id).exists())
