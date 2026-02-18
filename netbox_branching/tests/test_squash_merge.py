"""
Tests for Branch merge functionality with ObjectChange collapsing (squash strategy).
"""
import time
import uuid

from django.contrib.auth import get_user_model

from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse

from circuits.models import Circuit, CircuitTermination, CircuitType, Provider
from dcim.models import Region, Site
from netbox.context_managers import event_tracking
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models import Branch
from netbox_branching.utilities import activate_branch
from netbox_branching.tests.test_iterative_merge import BaseMergeTests


User = get_user_model()


class SquashMergeTestCase(BaseMergeTests, TransactionTestCase):
    """Test cases for Branch merge with ObjectChange collapsing and ordering."""

    def _create_and_provision_branch(self, name='Test Branch'):
        """Helper to create and provision a branch."""

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
