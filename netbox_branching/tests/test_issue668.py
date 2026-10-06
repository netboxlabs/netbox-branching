"""
Reproduction for GitHub issue #668.

Deleting a Device whose Interfaces have assigned/primary MACAddresses produces a
two-node DELETE cycle that the squash merge dependency resolver cannot order:

    Interface  --primary_mac_address (nullable FK)--> MACAddress
    MACAddress --assigned_object (GFK)--------------> Interface

_break_dependency_cycles() only breaks cycles among CREATEs, so this DELETE cycle
reaches the topological sort and aborts the merge.
"""

import uuid

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse

from dcim.models import Device, Interface, MACAddress, Site
from netbox.context_managers import event_tracking
from netbox_branching.choices import BranchMergeStrategyChoices, BranchStatusChoices
from netbox_branching.utilities import activate_branch

from .test_iterative_merge import BaseMergeTests

User = get_user_model()


class Issue668MergeTests(BaseMergeTests):
    """Merging a device deletion where the interface carries a primary MAC address."""

    def _make_request(self):
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user
        return request

    def test_merge_delete_device_with_primary_mac_addresses(self):
        # Build the device topology in MAIN so the branch inherits it.
        with event_tracking(self._make_request()):
            site = Site.objects.create(name='Test Site', slug='test-site')
            device = Device.objects.create(
                name='Test Device',
                site=site,
                device_type=self.device_type,
                role=self.device_role,
            )
            iface = Interface.objects.create(device=device, name='em0', type='virtual')
            mac = MACAddress.objects.create(mac_address='00:11:22:33:44:55', assigned_object=iface)
            iface.snapshot()
            iface.primary_mac_address = mac
            iface.save()

        device_id, iface_id, mac_id = device.id, iface.id, mac.id

        branch = self._create_and_provision_branch()

        # Sanity check: the branch sees the same topology.
        with activate_branch(branch):
            self.assertTrue(Device.objects.filter(id=device_id).exists())
            self.assertEqual(Interface.objects.get(id=iface_id).primary_mac_address_id, mac_id)
            self.assertEqual(MACAddress.objects.get(id=mac_id).assigned_object_id, iface_id)

        # In the branch, delete just the device. Django cascades to the Interface, and
        # the Interface's mac_addresses GenericRelation cascades to the MACAddress.
        with activate_branch(branch), event_tracking(self._make_request()):
            Device.objects.get(id=device_id).delete()

        with activate_branch(branch):
            self.assertFalse(Device.objects.filter(id=device_id).exists())
            self.assertFalse(Interface.objects.filter(id=iface_id).exists())
            self.assertFalse(MACAddress.objects.filter(id=mac_id).exists())

        # Squash merge: issue #668 reports "Cycle detected in dependency graph" here.
        branch.merge(user=self.user, commit=True)

        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)
        self.assertFalse(Device.objects.filter(id=device_id).exists())
        self.assertFalse(Interface.objects.filter(id=iface_id).exists())
        self.assertFalse(MACAddress.objects.filter(id=mac_id).exists())


class Issue668SquashMergeTestCase(Issue668MergeTests, TransactionTestCase):
    MERGE_STRATEGY = BranchMergeStrategyChoices.SQUASH


class Issue668IterativeMergeTestCase(Issue668MergeTests, TransactionTestCase):
    """Control: the iterative strategy replays changes in order and has no dependency graph."""

    MERGE_STRATEGY = BranchMergeStrategyChoices.ITERATIVE
