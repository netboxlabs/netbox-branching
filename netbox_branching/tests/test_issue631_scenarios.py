"""
Closer reproductions of netbox-branching issue #631, used to confirm that the
NetBox fix for netbox-community/netbox#23160 covers the scenario as reported:
a FrontPort (not just an Interface), a disconnect that happens in its own
request, and unrelated changes accumulated afterwards in a long-lived branch.
"""
import uuid

from dcim.models import (
    Cable,
    Device,
    DeviceRole,
    DeviceType,
    FrontPort,
    Interface,
    Manufacturer,
    PortMapping,
    RearPort,
    Site,
)
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


class Issue631Mixin:
    MERGE_STRATEGY = None
    serialized_rollback = True

    def setUp(self):
        self.user = User.objects.create_user(username='testuser')

        with event_tracking(self._new_request()):
            manufacturer = Manufacturer.objects.create(name='Manufacturer 1', slug='manufacturer-1')
            device_type = DeviceType.objects.create(
                manufacturer=manufacturer, model='Device Type 1', slug='device-type-1'
            )
            role = DeviceRole.objects.create(name='Device Role 1', slug='device-role-1')
            self.site = Site.objects.create(name='Site 1', slug='site-1')
            device_a = Device.objects.create(name='Device A', site=self.site, device_type=device_type, role=role)
            device_b = Device.objects.create(name='Device B', site=self.site, device_type=device_type, role=role)
            self.rear_port = RearPort.objects.create(device=device_a, name='rear0', type='8p8c', positions=1)
            self.front_port = FrontPort.objects.create(device=device_a, name='front0', type='8p8c', positions=1)
            PortMapping.objects.create(front_port=self.front_port, rear_port=self.rear_port)
            self.interface = Interface.objects.create(device=device_b, name='eth0', type='1000base-t')

    def tearDown(self):
        for branch in Branch.objects.all():
            if hasattr(connections._connections, branch.connection_name):
                connections[branch.connection_name].close()

    def _new_request(self):
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user
        return request

    def _connect(self):
        cable = Cable(
            a_terminations=[FrontPort.objects.get(pk=self.front_port.pk)],
            b_terminations=[Interface.objects.get(pk=self.interface.pk)],
        )
        cable.save()
        return cable.pk

    def _accumulate_unrelated_changes(self, count=5):
        for i in range(count):
            Site.objects.create(name=f'Unrelated Site {i}', slug=f'unrelated-site-{i}')

    def test_frontport_connect_then_disconnect_in_branch(self):
        """
        The reported flow: connect and disconnect a FrontPort inside the branch in
        separate requests, then pile unrelated changes on top before merging.
        """
        branch = provision_branch(user=self.user, name='Test Branch', merge_strategy=self.MERGE_STRATEGY)

        with activate_branch(branch), event_tracking(self._new_request()):
            cable_id = self._connect()

        with activate_branch(branch), event_tracking(self._new_request()):
            Cable.objects.get(pk=cable_id).delete()

        with activate_branch(branch), event_tracking(self._new_request()):
            self._accumulate_unrelated_changes()

        with activate_branch(branch):
            self.assertIsNone(FrontPort.objects.get(pk=self.front_port.pk).cable_id)
            self.assertIsNone(Interface.objects.get(pk=self.interface.pk).cable_id)

        branch.merge(user=self.user, commit=True)

        self.assertIsNone(FrontPort.objects.get(pk=self.front_port.pk).cable_id)
        self.assertIsNone(Interface.objects.get(pk=self.interface.pk).cable_id)
        self.assertFalse(Cable.objects.filter(pk=cable_id).exists())
        self.assertEqual(Site.objects.filter(name__startswith='Unrelated Site').count(), 5)

    def test_frontport_disconnect_in_branch_of_cable_from_main(self):
        """
        Variant on the same mechanism: the cable already exists in main and the branch
        only disconnects it. The branch's record for the FrontPort is the disconnect
        itself, which is the change that used to be missing.
        """
        with event_tracking(self._new_request()):
            cable_id = self._connect()

        branch = provision_branch(user=self.user, name='Test Branch', merge_strategy=self.MERGE_STRATEGY)

        with activate_branch(branch), event_tracking(self._new_request()):
            Cable.objects.get(pk=cable_id).delete()

        with activate_branch(branch), event_tracking(self._new_request()):
            self._accumulate_unrelated_changes()

        branch.merge(user=self.user, commit=True)

        self.assertIsNone(FrontPort.objects.get(pk=self.front_port.pk).cable_id)
        self.assertIsNone(Interface.objects.get(pk=self.interface.pk).cable_id)
        self.assertFalse(Cable.objects.filter(pk=cable_id).exists())

    def test_frontport_reconnect_after_disconnect_in_branch(self):
        """
        Connect, disconnect, then connect again to a second cable inside the branch:
        the surviving reference has to be the live cable, not the deleted one.
        """
        branch = provision_branch(user=self.user, name='Test Branch', merge_strategy=self.MERGE_STRATEGY)

        with activate_branch(branch), event_tracking(self._new_request()):
            first_cable_id = self._connect()

        with activate_branch(branch), event_tracking(self._new_request()):
            Cable.objects.get(pk=first_cable_id).delete()

        with activate_branch(branch), event_tracking(self._new_request()):
            second_cable_id = self._connect()

        branch.merge(user=self.user, commit=True)

        self.assertFalse(Cable.objects.filter(pk=first_cable_id).exists())
        self.assertTrue(Cable.objects.filter(pk=second_cable_id).exists())
        self.assertEqual(FrontPort.objects.get(pk=self.front_port.pk).cable_id, second_cable_id)
        self.assertEqual(Interface.objects.get(pk=self.interface.pk).cable_id, second_cable_id)


class SquashIssue631TestCase(Issue631Mixin, TransactionTestCase):
    MERGE_STRATEGY = BranchMergeStrategyChoices.SQUASH


class IterativeIssue631TestCase(Issue631Mixin, TransactionTestCase):
    MERGE_STRATEGY = BranchMergeStrategyChoices.ITERATIVE
