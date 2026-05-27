"""
Tests for the plugin's pre/post lifecycle signals.

Each Branch lifecycle operation (provision, deprovision, sync, migrate, merge,
revert) emits matching pre_X / post_X signals that are part of the plugin's
public contract for third-party integrations. This module verifies that:

  * each signal fires with sender=Branch
  * branch= and user= kwargs match the documented contract
    (deprovision signals carry branch only — no user)
  * post_X is skipped when sync/merge/revert short-circuit on "no changes",
    while pre_X fires unconditionally
  * post_migrate fires even when there are no migrations to apply
"""
import uuid
from contextlib import contextmanager

from dcim.models import Site
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse
from netbox.context_managers import event_tracking

from netbox_branching import signals as branch_signals
from netbox_branching.models import Branch
from netbox_branching.tests.utils import provision_branch
from netbox_branching.utilities import activate_branch

User = get_user_model()


@contextmanager
def capture_signal(signal):
    """
    Connect a temporary handler that records each (sender, kwargs) call.

    weak=False matches the production connection pattern used in
    signal_receivers.py so we never observe a different connection mode than
    the plugin itself does.
    """
    received = []

    def handler(sender, **kwargs):
        received.append((sender, kwargs))

    signal.connect(handler, weak=False, dispatch_uid='signal_capture_test')
    try:
        yield received
    finally:
        signal.disconnect(dispatch_uid='signal_capture_test')


class BranchSignalTestCase(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.user = User.objects.create_user(username='signaltest')
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = self.user
        self.request = request

    def tearDown(self):
        for branch in Branch.objects.all():
            if hasattr(connections, branch.connection_name):
                connections[branch.connection_name].close()

    # -------------------------------------------------------------------------
    # provision / deprovision
    # -------------------------------------------------------------------------

    def test_provision_fires_pre_and_post(self):
        with (
            capture_signal(branch_signals.pre_provision) as pre,
            capture_signal(branch_signals.post_provision) as post,
        ):
            branch = provision_branch(user=self.user)

        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)
        self.assertIs(pre[0][0], Branch)
        self.assertIs(post[0][0], Branch)
        self.assertEqual(pre[0][1]['branch'].pk, branch.pk)
        self.assertEqual(pre[0][1]['user'], self.user)
        self.assertEqual(post[0][1]['branch'].pk, branch.pk)
        self.assertEqual(post[0][1]['user'], self.user)

    def test_deprovision_fires_without_user_kwarg(self):
        branch = provision_branch(user=self.user)

        with (
            capture_signal(branch_signals.pre_deprovision) as pre,
            capture_signal(branch_signals.post_deprovision) as post,
        ):
            branch.deprovision()

        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)
        self.assertEqual(pre[0][1]['branch'].pk, branch.pk)
        self.assertEqual(post[0][1]['branch'].pk, branch.pk)
        # deprovision is the only lifecycle operation that does not receive a user
        self.assertNotIn('user', pre[0][1])
        self.assertNotIn('user', post[0][1])

    # -------------------------------------------------------------------------
    # sync
    # -------------------------------------------------------------------------

    def test_sync_fires_pre_and_post_when_there_are_changes(self):
        branch = provision_branch(user=self.user)
        with event_tracking(self.request):
            Site.objects.create(name='Main Site', slug='main-site-sync')

        with (
            capture_signal(branch_signals.pre_sync) as pre,
            capture_signal(branch_signals.post_sync) as post,
        ):
            branch.sync(user=self.user)

        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)
        self.assertEqual(pre[0][1]['user'], self.user)
        self.assertEqual(post[0][1]['user'], self.user)

    def test_sync_skips_post_when_no_changes(self):
        """sync() returns early after pre_sync if there is nothing to apply."""
        branch = provision_branch(user=self.user)

        with (
            capture_signal(branch_signals.pre_sync) as pre,
            capture_signal(branch_signals.post_sync) as post,
        ):
            branch.sync(user=self.user)

        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 0)

    # -------------------------------------------------------------------------
    # migrate
    # -------------------------------------------------------------------------

    def test_migrate_fires_pre_and_post_with_empty_plan(self):
        """post_migrate fires even when no migrations are pending."""
        branch = provision_branch(user=self.user)

        with (
            capture_signal(branch_signals.pre_migrate) as pre,
            capture_signal(branch_signals.post_migrate) as post,
        ):
            branch.migrate(user=self.user)

        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)

    # -------------------------------------------------------------------------
    # merge / revert
    # -------------------------------------------------------------------------

    def test_merge_fires_pre_and_post_when_there_are_changes(self):
        branch = provision_branch(user=self.user)
        with activate_branch(branch), event_tracking(self.request):
            Site.objects.create(name='Branch Site', slug='branch-site-merge')

        with (
            capture_signal(branch_signals.pre_merge) as pre,
            capture_signal(branch_signals.post_merge) as post,
        ):
            branch.merge(user=self.user)

        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)
        self.assertEqual(pre[0][1]['user'], self.user)
        self.assertEqual(post[0][1]['user'], self.user)

    def test_merge_skips_post_when_no_changes(self):
        """merge() returns early after pre_merge if the branch has no changes."""
        branch = provision_branch(user=self.user)

        with (
            capture_signal(branch_signals.pre_merge) as pre,
            capture_signal(branch_signals.post_merge) as post,
        ):
            branch.merge(user=self.user)

        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 0)

    def test_revert_fires_pre_and_post(self):
        branch = provision_branch(user=self.user)
        with activate_branch(branch), event_tracking(self.request):
            Site.objects.create(name='Revertible Site', slug='revertible-site')
        branch.merge(user=self.user)

        with (
            capture_signal(branch_signals.pre_revert) as pre,
            capture_signal(branch_signals.post_revert) as post,
        ):
            branch.revert(user=self.user)

        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)
        self.assertEqual(pre[0][1]['user'], self.user)
        self.assertEqual(post[0][1]['user'], self.user)
