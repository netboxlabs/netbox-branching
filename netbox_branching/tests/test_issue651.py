"""
Reproduction for netbox-branching issue #651.

Create a CircuitTermination inside a branch and merge. The Circuit's denormalized
termination_a/termination_z pointer is written by CircuitTermination.save(); branching
replays a CREATE via DeserializedObject.save() (raw), so that method never runs in main.
Before the NetBox fix the pointer was written with a queryset update() which emitted no
ObjectChange either, so nothing carried the association across and main was left with a
circuit whose termination pointer was null.
"""
import uuid

from circuits.models import Circuit, CircuitTermination, CircuitType, Provider
from dcim.models import Site
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse
from netbox.context_managers import event_tracking

from netbox_branching.choices import BranchMergeStrategyChoices
from netbox_branching.models import Branch
from netbox_branching.utilities import activate_branch

from .utils import provision_branch

User = get_user_model()


class CreateCircuitTerminationInBranchMixin:
    MERGE_STRATEGY = None
    serialized_rollback = True

    def setUp(self):
        self.user = User.objects.create_user(username='testuser')

        with event_tracking(self._new_request()):
            provider = Provider.objects.create(name='Provider 1', slug='provider-1')
            circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
            self.circuit = Circuit.objects.create(cid='Circuit 1', provider=provider, type=circuit_type)
            self.site_a = Site.objects.create(name='Site 1', slug='site-1')
            self.site_z = Site.objects.create(name='Site 2', slug='site-2')

    def tearDown(self):
        for branch in Branch.objects.all():
            if hasattr(connections._connections, branch.connection_name):
                connections[branch.connection_name].close()

    def _new_request(self):
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user
        return request

    def test_termination_pointer_survives_merge(self):
        branch = provision_branch(user=self.user, name='Test Branch', merge_strategy=self.MERGE_STRATEGY)
        circuit_id = self.circuit.pk
        site_a_id, site_z_id = self.site_a.pk, self.site_z.pk

        with activate_branch(branch), event_tracking(self._new_request()):
            term_a = CircuitTermination.objects.create(
                circuit=Circuit.objects.get(pk=circuit_id),
                term_side='A',
                termination=Site.objects.get(pk=site_a_id),
            )
            term_z = CircuitTermination.objects.create(
                circuit=Circuit.objects.get(pk=circuit_id),
                term_side='Z',
                termination=Site.objects.get(pk=site_z_id),
            )
            term_a_id, term_z_id = term_a.pk, term_z.pk

        # The branch itself is correct
        with activate_branch(branch):
            circuit = Circuit.objects.get(pk=circuit_id)
            self.assertEqual(circuit.termination_a_id, term_a_id)
            self.assertEqual(circuit.termination_z_id, term_z_id)

        branch.merge(user=self.user, commit=True)

        circuit = Circuit.objects.get(pk=circuit_id)
        self.assertEqual(CircuitTermination.objects.filter(circuit=circuit).count(), 2)
        self.assertEqual(circuit.termination_a_id, term_a_id)
        self.assertEqual(circuit.termination_z_id, term_z_id)

    def test_termination_pointer_cleared_by_merged_deletion(self):
        # The deletion half of the lifecycle is not change-logged: on_delete=SET_NULL emits no
        # post_save, and related_name='+' hides the relation from handle_deleted_object(). Replay
        # deletes through the ORM, so the collector re-applies SET_NULL in main.
        circuit_id = self.circuit.pk
        site_a_id = self.site_a.pk

        with event_tracking(self._new_request()):
            term_a = CircuitTermination.objects.create(
                circuit=Circuit.objects.get(pk=circuit_id),
                term_side='A',
                termination=Site.objects.get(pk=site_a_id),
            )
            term_a_id = term_a.pk

        branch = provision_branch(user=self.user, name='Test Branch', merge_strategy=self.MERGE_STRATEGY)

        with activate_branch(branch), event_tracking(self._new_request()):
            CircuitTermination.objects.get(pk=term_a_id).delete()

        with activate_branch(branch):
            self.assertIsNone(Circuit.objects.get(pk=circuit_id).termination_a_id)

        branch.merge(user=self.user, commit=True)

        self.assertFalse(CircuitTermination.objects.filter(pk=term_a_id).exists())
        self.assertIsNone(Circuit.objects.get(pk=circuit_id).termination_a_id)

    def test_termination_pointer_restored_by_revert(self):
        # Undoing the merged deletion restores the termination through its own save(), which
        # writes the pointer back.
        circuit_id = self.circuit.pk
        site_a_id = self.site_a.pk

        with event_tracking(self._new_request()):
            term_a = CircuitTermination.objects.create(
                circuit=Circuit.objects.get(pk=circuit_id),
                term_side='A',
                termination=Site.objects.get(pk=site_a_id),
            )
            term_a_id = term_a.pk

        branch = provision_branch(user=self.user, name='Test Branch', merge_strategy=self.MERGE_STRATEGY)

        with activate_branch(branch), event_tracking(self._new_request()):
            CircuitTermination.objects.get(pk=term_a_id).delete()

        branch.merge(user=self.user, commit=True)
        self.assertIsNone(Circuit.objects.get(pk=circuit_id).termination_a_id)

        branch.revert(user=self.user, commit=True)

        self.assertTrue(CircuitTermination.objects.filter(pk=term_a_id).exists())
        self.assertEqual(Circuit.objects.get(pk=circuit_id).termination_a_id, term_a_id)

    def test_termination_pointer_survives_sync(self):
        # The reverse direction: terminations created in main must land in an existing branch.
        branch = provision_branch(user=self.user, name='Test Branch', merge_strategy=self.MERGE_STRATEGY)
        circuit_id = self.circuit.pk
        site_a_id, site_z_id = self.site_a.pk, self.site_z.pk

        with event_tracking(self._new_request()):
            term_a = CircuitTermination.objects.create(
                circuit=Circuit.objects.get(pk=circuit_id),
                term_side='A',
                termination=Site.objects.get(pk=site_a_id),
            )
            term_z = CircuitTermination.objects.create(
                circuit=Circuit.objects.get(pk=circuit_id),
                term_side='Z',
                termination=Site.objects.get(pk=site_z_id),
            )
            term_a_id, term_z_id = term_a.pk, term_z.pk

        branch.sync(user=self.user, commit=True)

        with activate_branch(branch):
            circuit = Circuit.objects.get(pk=circuit_id)
            self.assertEqual(circuit.termination_a_id, term_a_id)
            self.assertEqual(circuit.termination_z_id, term_z_id)

    def test_termination_pointer_removed_by_revert(self):
        # Reverting the merged branch removes the terminations again, so the circuit's pointers
        # must come back to null rather than dangling.
        branch = provision_branch(user=self.user, name='Test Branch', merge_strategy=self.MERGE_STRATEGY)
        circuit_id = self.circuit.pk
        site_a_id, site_z_id = self.site_a.pk, self.site_z.pk

        with activate_branch(branch), event_tracking(self._new_request()):
            term_a = CircuitTermination.objects.create(
                circuit=Circuit.objects.get(pk=circuit_id),
                term_side='A',
                termination=Site.objects.get(pk=site_a_id),
            )
            term_z = CircuitTermination.objects.create(
                circuit=Circuit.objects.get(pk=circuit_id),
                term_side='Z',
                termination=Site.objects.get(pk=site_z_id),
            )
            term_a_id, term_z_id = term_a.pk, term_z.pk

        branch.merge(user=self.user, commit=True)

        circuit = Circuit.objects.get(pk=circuit_id)
        self.assertEqual(circuit.termination_a_id, term_a_id)
        self.assertEqual(circuit.termination_z_id, term_z_id)

        branch.revert(user=self.user, commit=True)

        circuit = Circuit.objects.get(pk=circuit_id)
        self.assertFalse(CircuitTermination.objects.filter(pk__in=[term_a_id, term_z_id]).exists())
        self.assertIsNone(circuit.termination_a_id)
        self.assertIsNone(circuit.termination_z_id)


class SquashCircuitTerminationTestCase(CreateCircuitTerminationInBranchMixin, TransactionTestCase):
    MERGE_STRATEGY = BranchMergeStrategyChoices.SQUASH


class IterativeCircuitTerminationTestCase(CreateCircuitTerminationInBranchMixin, TransactionTestCase):
    MERGE_STRATEGY = BranchMergeStrategyChoices.ITERATIVE
