"""
Tests for the background-job layer (`netbox_branching.jobs`).

The branch lifecycle methods are well-tested through model-level tests; what
this module covers is the **wrapping behaviour** the Job classes add — most
critically:
  * the signal-disconnect context manager that protects sync/merge/migrate
    from generating spurious ObjectChange records (issue #542)
  * the dry-run paths (commit=False raises AbortTransaction, which each Job
    must catch silently so the job log records "dry run completed" instead
    of "failed")
  * MergeBranchJob's error-classification path (IntegrityError / ValidationError
    are routed through build_error_report and re-raised so the job ends in
    a FAILED state with a structured report attached to job.data)
"""
import weakref
from types import SimpleNamespace
from unittest import mock

from core.models import ObjectChange
from core.signals import handle_changed_object, handle_deleted_object
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models.signals import m2m_changed, post_save, pre_delete
from django.test import SimpleTestCase, TestCase
from netbox.signals import post_clean
from utilities.exceptions import AbortTransaction

from netbox_branching.jobs import (
    MergeBranchJob,
    MigrateBranchJob,
    RevertBranchJob,
    SyncBranchJob,
    disconnect_object_change_signal_handlers,
)
from netbox_branching.models import Branch
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


class JobRunWrapperTestCase(TestCase):
    """
    Job.run() wraps each lifecycle method with logging, dry-run handling, and
    (for MergeBranchJob) error-report building. These tests substitute mocks
    for the underlying Branch method so we exercise only the wrapper layer —
    real merge/sync logic is covered by test_iterative_merge / test_sync.
    """

    def setUp(self):
        # status=NEW means get_unmerged_changes() / get_unsynced_changes() both
        # return ObjectChange.objects.none(), so _snapshot_changes_summary safely
        # iterates an empty queryset and we avoid needing real provisioning.
        self.branch = Branch(name='Job Wrapper Test')
        self.branch.save(provision=False)

    def _make_job_stub(self):
        return SimpleNamespace(object=self.branch, user=None, data={})

    # SyncBranchJob ------------------------------------------------------------

    def test_sync_job_swallows_abort_transaction(self):
        """commit=False raises AbortTransaction inside Branch.sync; the job must catch it."""
        job_stub = self._make_job_stub()
        with mock.patch.object(Branch, 'sync', side_effect=AbortTransaction):
            SyncBranchJob(job_stub).run(commit=False)  # must not raise

    def test_sync_job_propagates_other_exceptions(self):
        """A non-AbortTransaction failure should bubble up so the job is marked failed."""
        job_stub = self._make_job_stub()
        with (
            mock.patch.object(Branch, 'sync', side_effect=RuntimeError('boom')),
            self.assertRaises(RuntimeError),
        ):
            SyncBranchJob(job_stub).run()

    # MergeBranchJob -----------------------------------------------------------

    def test_merge_job_initializes_data_fields(self):
        """run() seeds report/changes_summary/has_unsynced_changes/merge_strategy on job.data."""
        job_stub = self._make_job_stub()
        with mock.patch.object(Branch, 'merge'):
            MergeBranchJob(job_stub).run()
        self.assertEqual(job_stub.data['report'], [])
        self.assertEqual(job_stub.data['changes_summary']['creates_total'], 0)
        self.assertEqual(job_stub.data['changes_summary']['updates_total'], 0)
        self.assertEqual(job_stub.data['changes_summary']['deletes_total'], 0)
        self.assertFalse(job_stub.data['has_unsynced_changes'])
        self.assertIn('merge_strategy', job_stub.data)

    def test_merge_job_swallows_abort_transaction(self):
        job_stub = self._make_job_stub()
        with mock.patch.object(Branch, 'merge', side_effect=AbortTransaction):
            MergeBranchJob(job_stub).run(commit=False)
        self.assertEqual(job_stub.data['report'], [])

    def test_merge_job_records_integrity_error_in_report_and_reraises(self):
        """
        IntegrityError caught in merge must be routed through build_error_report
        so the job log shows a structured entry, then re-raised so the job ends
        in a FAILED state.
        """
        job_stub = self._make_job_stub()
        exc = IntegrityError('duplicate key value violates unique constraint')
        # No __cause__ — falls through to generic database_error classification
        with (
            mock.patch.object(Branch, 'merge', side_effect=exc),
            self.assertRaises(IntegrityError),
        ):
            MergeBranchJob(job_stub).run()
        self.assertEqual(len(job_stub.data['report']), 1)
        self.assertEqual(job_stub.data['report'][0]['type'], 'database_error')

    def test_merge_job_records_validation_error_in_report_and_reraises(self):
        job_stub = self._make_job_stub()
        exc = ValidationError({'slug': [ValidationError('duplicate', code='unique')]})
        with (
            mock.patch.object(Branch, 'merge', side_effect=exc),
            self.assertRaises(ValidationError),
        ):
            MergeBranchJob(job_stub).run()
        self.assertEqual(len(job_stub.data['report']), 1)
        self.assertEqual(job_stub.data['report'][0]['type'], 'unique_constraint')

    # RevertBranchJob ----------------------------------------------------------

    def test_revert_job_swallows_abort_transaction(self):
        job_stub = self._make_job_stub()
        with mock.patch.object(Branch, 'revert', side_effect=AbortTransaction):
            RevertBranchJob(job_stub).run(commit=False)

    # MigrateBranchJob ---------------------------------------------------------

    def test_migrate_job_swallows_abort_transaction(self):
        """
        MigrateBranchJob.run() has no ``commit`` parameter — Branch.migrate()
        does not currently support dry-run, so no production path triggers
        AbortTransaction here. The job nevertheless wraps the call in
        ``except AbortTransaction`` defensively, mirroring the other lifecycle
        jobs; this test pins that protection so a refactor that drops the
        try/except can't go unnoticed (and so adding a real dry-run mode in
        the future starts from a known-good baseline).
        """
        job_stub = self._make_job_stub()
        with mock.patch.object(Branch, 'migrate', side_effect=AbortTransaction):
            MigrateBranchJob(job_stub).run()

    # _snapshot_changes_summary ------------------------------------------------

    def test_snapshot_changes_summary_handles_empty_queryset(self):
        """The aggregation must work over an empty queryset without choking on the GROUP BY."""
        summary = MergeBranchJob._snapshot_changes_summary(ObjectChange.objects.none())
        self.assertEqual(summary['creates'], {})
        self.assertEqual(summary['updates'], {})
        self.assertEqual(summary['deletes'], {})
        self.assertEqual(summary['creates_total'], 0)
        self.assertEqual(summary['updates_total'], 0)
        self.assertEqual(summary['deletes_total'], 0)
