"""
Tests for the background-job layer (`netbox_branching.jobs`).

The branch lifecycle methods are well-tested through model-level tests; what
this module covers is the **wrapping behaviour** the Job classes add — most
critically, the signal-disconnect context manager that protects sync/merge/
migrate from generating spurious ObjectChange records (regressions here have
historical precedent — see issue #542).
"""
import weakref

from core.signals import handle_changed_object, handle_deleted_object
from django.db.models.signals import m2m_changed, post_save, pre_delete
from django.test import SimpleTestCase
from netbox.signals import post_clean

from netbox_branching.jobs import disconnect_object_change_signal_handlers
from netbox_branching.signal_receivers import validate_branching_operations


def _receivers_for(signal):
    """Return the set of currently-connected receivers on a signal."""
    result = set()
    for entry in signal.receivers:
        ref = entry[1]
        receiver = ref() if isinstance(ref, weakref.ReferenceType) else ref
        if receiver is not None:
            result.add(receiver)
    return result


def _all_handlers_connected():
    return (
        handle_changed_object in _receivers_for(post_save)
        and handle_changed_object in _receivers_for(m2m_changed)
        and handle_deleted_object in _receivers_for(pre_delete)
        and validate_branching_operations in _receivers_for(post_clean)
    )


class DisconnectObjectChangeSignalHandlersTestCase(SimpleTestCase):
    """
    The context manager is what stops branch-internal sync/merge writes from
    generating ObjectChange records (and from triggering the branching
    validators) while still leaving normal request-path writes fully audited.

    If reconnection on exception ever silently breaks, *every subsequent
    write* to the NetBox process would stop producing ObjectChange records —
    a corruption that no functional test would notice until a user tried to
    diff their branch and saw nothing. These tests pin down the contract.
    """

    def test_handlers_are_connected_before_entering_context(self):
        """Sanity check the precondition the other tests depend on."""
        self.assertTrue(
            _all_handlers_connected(),
            "Expected object-change signal handlers to be connected at test start; "
            "another test likely failed to clean up.",
        )

    def test_handlers_are_disconnected_inside_context(self):
        with disconnect_object_change_signal_handlers():
            self.assertNotIn(handle_changed_object, _receivers_for(post_save))
            self.assertNotIn(handle_changed_object, _receivers_for(m2m_changed))
            self.assertNotIn(handle_deleted_object, _receivers_for(pre_delete))
            self.assertNotIn(validate_branching_operations, _receivers_for(post_clean))
        # Restored after normal exit
        self.assertTrue(_all_handlers_connected())

    def test_handlers_reconnect_when_block_raises(self):
        """
        The reconnection logic lives in `finally`, so any exception inside the
        block must still trigger it. Without this guarantee a single failed
        sync/migrate job would corrupt the changelog for the remainder of the
        process lifetime.
        """
        with (
            self.assertRaises(RuntimeError),
            disconnect_object_change_signal_handlers(),
        ):
            self.assertNotIn(handle_changed_object, _receivers_for(post_save))
            raise RuntimeError('boom inside disconnect block')
        self.assertTrue(_all_handlers_connected())
