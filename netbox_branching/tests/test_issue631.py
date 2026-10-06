"""
Reproduction for netbox-branching issue #631.

Connect a cable to a terminating object inside a branch, then disconnect it
(delete the Cable) inside the same branch. The cascaded CableTermination delete
nullifies the terminating object's denormalized `cable` field via a queryset
update (dcim.signals.nullify_connected_endpoints), which emits no ObjectChange.
The branch is therefore left with a single "update" change for the terminating
object pointing at a Cable which no longer exists, and merging fails.
"""

import uuid

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connections
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse

from dcim.models import Cable, CableTermination, Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from netbox.context_managers import event_tracking
from netbox_branching.choices import BranchMergeStrategyChoices
from netbox_branching.models import Branch
from netbox_branching.utilities import activate_branch

from .utils import provision_branch

User = get_user_model()


class ConnectThenDisconnectMixin:
    MERGE_STRATEGY = None
    serialized_rollback = True

    def setUp(self):
        self.user = User.objects.create_user(username='testuser')
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user

        with event_tracking(request):
            manufacturer = Manufacturer.objects.create(name='Manufacturer 1', slug='manufacturer-1')
            self.device_type = DeviceType.objects.create(
                manufacturer=manufacturer, model='Device Type 1', slug='device-type-1'
            )
            self.device_role = DeviceRole.objects.create(name='Device Role 1', slug='device-role-1')
            site = Site.objects.create(name='Site 1', slug='site-1')
            device_a = Device.objects.create(
                name='Device A', site=site, device_type=self.device_type, role=self.device_role
            )
            device_b = Device.objects.create(
                name='Device B', site=site, device_type=self.device_type, role=self.device_role
            )
            self.interface_a = Interface.objects.create(device=device_a, name='eth0', type='1000base-t')
            self.interface_b = Interface.objects.create(device=device_b, name='eth0', type='1000base-t')

    def tearDown(self):
        for branch in Branch.objects.all():
            if hasattr(connections._connections, branch.connection_name):
                connections[branch.connection_name].close()

    def _new_request(self):
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user
        return request

    def test_connect_then_disconnect_in_branch(self):
        branch = provision_branch(user=self.user, name='Test Branch', merge_strategy=self.MERGE_STRATEGY)

        interface_a_id = self.interface_a.pk
        interface_b_id = self.interface_b.pk

        # Step 2: activate the branch, then connect a cable
        with activate_branch(branch), event_tracking(self._new_request()):
            cable = Cable(
                a_terminations=[Interface.objects.get(pk=interface_a_id)],
                b_terminations=[Interface.objects.get(pk=interface_b_id)],
            )
            cable.save()
            cable_id = cable.pk

        # Step 3: disconnect it again (delete the cable) in a separate request
        with activate_branch(branch), event_tracking(self._new_request()):
            Cable.objects.get(pk=cable_id).delete()

        # The branch's current state is correct: the interface is uncabled
        with activate_branch(branch):
            self.assertIsNone(Interface.objects.get(pk=interface_a_id).cable_id)
            self.assertFalse(Cable.objects.filter(pk=cable_id).exists())

        # Dump the recorded changes for the interface
        iface_ct = ContentType.objects.get_for_model(Interface)
        cable_ct = ContentType.objects.get_for_model(Cable)
        ct_ct = ContentType.objects.get_for_model(CableTermination)
        print(f'\n--- Unmerged changes in branch (strategy={self.MERGE_STRATEGY}) ---')
        for change in branch.get_unmerged_changes().order_by('time'):
            extra = ''
            if change.changed_object_type_id == iface_ct.pk:
                extra = (
                    f'  pre.cable={(change.prechange_data or {}).get("cable")}'
                    f'  post.cable={(change.postchange_data or {}).get("cable")}'
                )
            print(
                f'{change.time}  {change.action:8s} {change.changed_object_type.model:18s} '
                f'id={change.changed_object_id}{extra}'
            )
        print(f'cable_id={cable_id}, interface_a={interface_a_id}, interface_b={interface_b_id}')
        print(
            'counts: '
            f'iface={branch.get_unmerged_changes().filter(changed_object_type=iface_ct).count()} '
            f'cable={branch.get_unmerged_changes().filter(changed_object_type=cable_ct).count()} '
            f'cabletermination={branch.get_unmerged_changes().filter(changed_object_type=ct_ct).count()}'
        )
        print('--- end changes ---\n')

        # Step 5: merge
        branch.merge(user=self.user, commit=True)

        # If we get here, the merge succeeded; main should be uncabled
        self.assertIsNone(Interface.objects.get(pk=interface_a_id).cable_id)
        self.assertFalse(Cable.objects.filter(pk=cable_id).exists())


class SquashConnectDisconnectTestCase(ConnectThenDisconnectMixin, TransactionTestCase):
    MERGE_STRATEGY = BranchMergeStrategyChoices.SQUASH


class IterativeConnectDisconnectTestCase(ConnectThenDisconnectMixin, TransactionTestCase):
    MERGE_STRATEGY = BranchMergeStrategyChoices.ITERATIVE


class SameRequestMixin(ConnectThenDisconnectMixin):
    """
    Variant: the cable is connected and disconnected within a single request, so the
    Cable's create+delete collapse to nothing in the changelog while the terminating
    object keeps an update change pointing at the (never-created) Cable.
    """

    def test_connect_then_disconnect_in_branch(self):
        branch = provision_branch(user=self.user, name='Test Branch', merge_strategy=self.MERGE_STRATEGY)
        interface_a_id = self.interface_a.pk
        interface_b_id = self.interface_b.pk

        with activate_branch(branch), event_tracking(self._new_request()):
            cable = Cable(
                a_terminations=[Interface.objects.get(pk=interface_a_id)],
                b_terminations=[Interface.objects.get(pk=interface_b_id)],
            )
            cable.save()
            cable_id = cable.pk
            Cable.objects.get(pk=cable_id).delete()

        print(f'\n--- Unmerged changes (same request, strategy={self.MERGE_STRATEGY}) ---')
        iface_ct = ContentType.objects.get_for_model(Interface)
        for change in branch.get_unmerged_changes().order_by('time'):
            extra = ''
            if change.changed_object_type_id == iface_ct.pk:
                extra = (
                    f'  pre.cable={(change.prechange_data or {}).get("cable")}'
                    f'  post.cable={(change.postchange_data or {}).get("cable")}'
                )
            print(f'{change.action:8s} {change.changed_object_type.model:18s} id={change.changed_object_id}{extra}')
        print('--- end changes ---\n')

        branch.merge(user=self.user, commit=True)

        self.assertIsNone(Interface.objects.get(pk=interface_a_id).cable_id)


class SquashSameRequestTestCase(SameRequestMixin, TransactionTestCase):
    MERGE_STRATEGY = BranchMergeStrategyChoices.SQUASH


class IterativeSameRequestTestCase(SameRequestMixin, TransactionTestCase):
    MERGE_STRATEGY = BranchMergeStrategyChoices.ITERATIVE
