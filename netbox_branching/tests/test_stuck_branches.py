"""
Tests for the recovery of branches left in a transitional status by a job which never finished
(issue #622).

A branch operation writes its transitional status (provisioning, syncing, migrating, merging,
reverting) to the database before it starts, and clears it again from inside the worker process —
either on success or in an exception handler. A worker killed outright (an OOM kill, an evicted
container, `kill -9`) runs neither path, so the branch keeps that status forever: every action in
the UI is disabled and the only remaining fix is a manual UPDATE.

What is covered here:
  * `is_job_abandoned()` — deciding whether a job which claims to be running actually is
  * `Branch.check_stuck()` / `recover()` / `force_recover()` — detection and the status reset
  * `RecoverStuckBranchesJob` — the hourly watchdog which applies the reset unattended
  * the UI and REST API recovery actions

None of the branches here are provisioned; only status transitions matter, and the Job records are
synthesised rather than executed, since the whole point is a job which never ran to completion.
"""
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from core.choices import JobStatusChoices
from core.models import Job
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connections
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from users.models import Token

from netbox_branching.choices import BranchStatusChoices
from netbox_branching.jobs import (
    MergeBranchJob,
    MigrateBranchJob,
    ProvisionBranchJob,
    RecoverStuckBranchesJob,
    RevertBranchJob,
    SyncBranchJob,
    get_job_class_for_status,
)
from netbox_branching.models import Branch
from netbox_branching.utilities import RQ_JOB_MISSING, _get_tracked_branch_aliases, is_job_abandoned

User = get_user_model()

# A config which leaves recovery enabled with predictable timings. override_settings replaces
# PLUGINS_CONFIG wholesale, so every parameter the code under test reads must be repeated here.
RECOVERY_CONFIG = {
    'netbox_branching': {
        'job_timeout': 3600,
        'stuck_job_grace_period': 300,
        'auto_recover_stuck_branches': True,
    }
}


def make_job(branch, name, status=JobStatusChoices.STATUS_RUNNING, started_ago=timedelta(minutes=5)):
    """
    Create a Job record attached to `branch`, as a branch operation would.
    """
    return Job.objects.create(
        object_type=ContentType.objects.get_for_model(Branch),
        object_id=branch.pk,
        name=name,
        status=status,
        started=timezone.now() - started_ago if status != JobStatusChoices.STATUS_PENDING else None,
        job_id=uuid.uuid4(),
    )


def make_branch(name, status):
    branch = Branch(name=name, status=status)
    branch.save(provision=False)
    return branch


class BranchConnectionCleanupMixin:
    """
    None of the branches here are provisioned, so anything which reaches into a branch's own schema
    — rendering the branch view, which checks it for pending migrations — opens a connection whose
    search_path names a schema that does not exist. That connection uses its own alias, so it is
    covered neither by TestCase's transaction nor by its teardown; left open, it is handed to
    whichever test runs next and fails it with "relation ... does not exist". Close them here.
    """
    def tearDown(self):
        super().tearDown()
        aliases = _get_tracked_branch_aliases()
        for alias in list(aliases):
            connections[alias].close()
        aliases.clear()


class IsJobAbandonedTestCase(BranchConnectionCleanupMixin, TestCase):
    """
    The decision of whether a job which still reports itself as running is actually being executed.
    RQ is consulted first; where it cannot answer, elapsed runtime is used, on the basis that RQ
    kills a job which outlives its timeout, so nothing can outlive it and still be running.
    """

    def setUp(self):
        self.branch = make_branch('Job Under Test', BranchStatusChoices.MIGRATING)

    def test_terminated_job_is_abandoned(self):
        for status in JobStatusChoices.TERMINAL_STATE_CHOICES:
            with self.subTest(status=status):
                job = make_job(self.branch, MigrateBranchJob.Meta.name, status=status)
                self.assertTrue(is_job_abandoned(job))

    def test_job_missing_from_rq_is_abandoned(self):
        job = make_job(self.branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            self.assertTrue(is_job_abandoned(job))

    def test_dead_rq_statuses_are_abandoned(self):
        job = make_job(self.branch, MigrateBranchJob.Meta.name)
        for rq_status in ('failed', 'stopped', 'canceled'):
            with (
                self.subTest(rq_status=rq_status),
                patch('netbox_branching.utilities._get_rq_job_status', return_value=rq_status),
            ):
                self.assertTrue(is_job_abandoned(job))

    def test_queued_job_is_not_abandoned(self):
        # A job still waiting for a worker has not been abandoned, however long it has waited.
        job = make_job(self.branch, MigrateBranchJob.Meta.name, started_ago=timedelta(days=1))
        for rq_status in ('queued', 'deferred', 'scheduled'):
            with (
                self.subTest(rq_status=rq_status),
                patch('netbox_branching.utilities._get_rq_job_status', return_value=rq_status),
            ):
                self.assertFalse(is_job_abandoned(job))

    @override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
    def test_started_job_within_timeout_is_not_abandoned(self):
        job = make_job(self.branch, MigrateBranchJob.Meta.name, started_ago=timedelta(minutes=10))
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            self.assertFalse(is_job_abandoned(job, grace_period=300))

    @override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
    def test_started_job_beyond_timeout_is_abandoned(self):
        # job_timeout (3600s) + grace (300s) have elapsed, so RQ would have killed a live job.
        job = make_job(self.branch, MigrateBranchJob.Meta.name, started_ago=timedelta(hours=2))
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            self.assertTrue(is_job_abandoned(job, grace_period=300))

    @override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
    def test_falls_back_to_elapsed_time_when_rq_is_unavailable(self):
        recent = make_job(self.branch, MigrateBranchJob.Meta.name, started_ago=timedelta(minutes=10))
        stale = make_job(self.branch, MigrateBranchJob.Meta.name, started_ago=timedelta(hours=2))
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=None):
            self.assertFalse(is_job_abandoned(recent, grace_period=300))
            self.assertTrue(is_job_abandoned(stale, grace_period=300))

    @override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
    def test_pending_job_is_not_abandoned_when_rq_is_unavailable(self):
        # Without an answer from RQ, a job which has not started running cannot be timed out.
        job = make_job(self.branch, MigrateBranchJob.Meta.name, status=JobStatusChoices.STATUS_PENDING)
        Job.objects.filter(pk=job.pk).update(created=timezone.now() - timedelta(days=1))
        job.refresh_from_db()
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=None):
            self.assertFalse(is_job_abandoned(job, grace_period=300))


class JobClassLookupTestCase(BranchConnectionCleanupMixin, TestCase):

    def test_every_transitional_status_maps_to_a_job(self):
        for status in BranchStatusChoices.TRANSITIONAL:
            with self.subTest(status=status):
                self.assertIsNotNone(get_job_class_for_status(status))

    def test_every_transitional_status_has_a_recovery_status(self):
        for status in BranchStatusChoices.TRANSITIONAL:
            with self.subTest(status=status):
                self.assertIn(status, BranchStatusChoices.RECOVERY_STATUS)

    def test_non_transitional_status_maps_to_nothing(self):
        self.assertIsNone(get_job_class_for_status(BranchStatusChoices.READY))


@override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
class BranchStuckDetectionTestCase(BranchConnectionCleanupMixin, TestCase):

    def test_non_transitional_branch_is_never_stuck(self):
        for status in (BranchStatusChoices.READY, BranchStatusChoices.MERGED, BranchStatusChoices.FAILED):
            with self.subTest(status=status):
                branch = make_branch(f'Not Stuck {status}', status)
                self.assertEqual(branch.check_stuck(), (None, False))
                self.assertFalse(branch.is_stuck)

    def test_branch_with_no_job_record_is_stuck(self):
        # The Job record has been purged (or the operation was never queued); nothing remains to
        # clear the status.
        branch = make_branch('No Job', BranchStatusChoices.MIGRATING)
        self.assertEqual(branch.check_stuck(), (None, True))

    def test_branch_with_abandoned_job_is_stuck(self):
        branch = make_branch('Abandoned', BranchStatusChoices.MIGRATING)
        job = make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            self.assertEqual(branch.check_stuck(), (job, True))

    def test_branch_with_running_job_is_not_stuck(self):
        branch = make_branch('Running', BranchStatusChoices.MIGRATING)
        job = make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            self.assertEqual(branch.check_stuck(), (job, False))

    def test_only_the_job_matching_the_status_is_consulted(self):
        # A completed provisioning job must not be mistaken for the migration which left the
        # branch in its current status.
        branch = make_branch('Mixed Jobs', BranchStatusChoices.MIGRATING)
        make_job(branch, ProvisionBranchJob.Meta.name, status=JobStatusChoices.STATUS_COMPLETED)
        migrate_job = make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            self.assertEqual(branch.check_stuck(), (migrate_job, False))

    def test_latest_matching_job_is_used(self):
        branch = make_branch('Repeat Migrate', BranchStatusChoices.MIGRATING)
        make_job(branch, MigrateBranchJob.Meta.name, status=JobStatusChoices.STATUS_COMPLETED)
        latest = make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            self.assertEqual(branch.check_stuck(), (latest, False))


@override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
class BranchRecoveryTestCase(BranchConnectionCleanupMixin, TestCase):

    def _make_stuck(self, name, status):
        branch = make_branch(name, status)
        job = make_job(branch, get_job_class_for_status(status).Meta.name)
        return branch, job

    def test_recovery_status_per_transitional_status(self):
        expected = {
            BranchStatusChoices.PROVISIONING: BranchStatusChoices.FAILED,
            BranchStatusChoices.SYNCING: BranchStatusChoices.READY,
            BranchStatusChoices.MIGRATING: BranchStatusChoices.PENDING_MIGRATIONS,
            BranchStatusChoices.MERGING: BranchStatusChoices.READY,
            BranchStatusChoices.REVERTING: BranchStatusChoices.MERGED,
        }
        for status, recovered_status in expected.items():
            with self.subTest(status=status):
                branch, _ = self._make_stuck(f'Stuck {status}', status)
                with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
                    result = branch.recover()
                self.assertEqual(result, recovered_status)
                self.assertEqual(branch.status, recovered_status)
                branch.refresh_from_db()
                self.assertEqual(branch.status, recovered_status)

    def test_recovery_fails_the_orphaned_job(self):
        branch, job = self._make_stuck('Orphaned Job', BranchStatusChoices.MIGRATING)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            branch.recover()
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertIn(BranchStatusChoices.PENDING_MIGRATIONS, job.error)
        self.assertIsNotNone(job.completed)

    def test_recovery_preserves_an_already_terminated_job(self):
        # A job which errored out but left the status behind is itself evidence of the problem;
        # its recorded outcome must not be overwritten.
        branch = make_branch('Errored Job', BranchStatusChoices.MERGING)
        job = make_job(branch, MergeBranchJob.Meta.name, status=JobStatusChoices.STATUS_ERRORED)
        branch.recover()
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_ERRORED)
        self.assertEqual(branch.status, BranchStatusChoices.READY)

    def test_recover_is_a_no_op_for_a_live_job(self):
        branch, job = self._make_stuck('Live', BranchStatusChoices.SYNCING)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            self.assertIsNone(branch.recover())
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.SYNCING)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_RUNNING)

    def test_recover_is_a_no_op_for_a_non_transitional_branch(self):
        branch = make_branch('Ready Branch', BranchStatusChoices.READY)
        self.assertIsNone(branch.recover())
        self.assertEqual(branch.status, BranchStatusChoices.READY)

    def test_force_recover_overrides_a_live_job(self):
        branch, job = self._make_stuck('Forced', BranchStatusChoices.SYNCING)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            self.assertEqual(branch.force_recover(), BranchStatusChoices.READY)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)

    def test_force_recover_is_a_no_op_for_a_non_transitional_branch(self):
        branch = make_branch('Merged Branch', BranchStatusChoices.MERGED)
        self.assertIsNone(branch.force_recover())
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)


class RecoverStuckBranchesJobTestCase(BranchConnectionCleanupMixin, TestCase):
    """
    The hourly watchdog which applies recovery without anyone having to notice the problem first.
    """

    def _run(self):
        job = SimpleNamespace(object=None, user=None, data={}, log=lambda record: None)
        RecoverStuckBranchesJob(job).run()

    @override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
    def test_recovers_stuck_branches(self):
        branch = make_branch('Stuck Migrate', BranchStatusChoices.MIGRATING)
        make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            self._run()
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.PENDING_MIGRATIONS)

    @override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
    def test_never_reruns_the_interrupted_operation(self):
        # A worker killed by the operation itself would be killed by it again on every subsequent
        # sweep, so unattended recovery resets the status and stops there. Re-running is offered
        # only on the operator-initiated paths.
        branch = make_branch('Watchdog No Retry', BranchStatusChoices.SYNCING)
        make_job(branch, SyncBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING), \
                patch('netbox_branching.jobs.SyncBranchJob.enqueue') as enqueue:
            self._run()
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)
        enqueue.assert_not_called()

    @override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
    def test_leaves_branches_with_a_running_job(self):
        branch = make_branch('Busy Merge', BranchStatusChoices.MERGING)
        make_job(branch, MergeBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            self._run()
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGING)

    @override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
    def test_ignores_non_transitional_branches(self):
        branch = make_branch('Ready', BranchStatusChoices.READY)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            self._run()
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)

    @override_settings(PLUGINS_CONFIG={
        'netbox_branching': {
            'job_timeout': 3600,
            'stuck_job_grace_period': 300,
            'auto_recover_stuck_branches': False,
        }
    })
    def test_disabled_by_configuration(self):
        branch = make_branch('Stuck But Disabled', BranchStatusChoices.REVERTING)
        make_job(branch, RevertBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            self._run()
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.REVERTING)

    @override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
    def test_one_failure_does_not_abort_the_batch(self):
        first = make_branch('Aaa Broken', BranchStatusChoices.SYNCING)
        second = make_branch('Bbb Recoverable', BranchStatusChoices.SYNCING)
        make_job(second, SyncBranchJob.Meta.name)

        original = Branch.recover

        def flaky(self, user=None):
            if self.pk == first.pk:
                raise RuntimeError("boom")
            return original(self, user=user)

        with (
            patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING),
            patch.object(Branch, 'recover', flaky),
        ):
            self._run()

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, BranchStatusChoices.SYNCING)
        self.assertEqual(second.status, BranchStatusChoices.READY)


@override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
class BranchRecoverViewTestCase(BranchConnectionCleanupMixin, TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_user(username='recoverview_super', is_superuser=True)
        cls.unprivileged = User.objects.create_user(username='recoverview_user')

    def setUp(self):
        self.client.force_login(self.superuser)

    @staticmethod
    def _url(branch):
        return reverse('plugins:netbox_branching:branch_recover', kwargs={'pk': branch.pk})

    def test_get_renders_for_a_transitional_branch(self):
        branch = make_branch('Stuck View', BranchStatusChoices.MIGRATING)
        make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            response = self.client.get(self._url(branch))
        self.assertEqual(response.status_code, 200)

    def test_get_describes_what_recovery_does_for_this_status(self):
        # The confirmation text is status-specific: the operator needs to know what the interrupted
        # operation left behind before resetting it, and that differs per operation.
        for status, expected in (
            (BranchStatusChoices.MIGRATING, 'outstanding migrations can be applied'),
            (BranchStatusChoices.SYNCING, 'interrupted sync was rolled back'),
            (BranchStatusChoices.MERGING, 'nothing reached main'),
            (BranchStatusChoices.REVERTING, 'changes are still in main'),
            (BranchStatusChoices.PROVISIONING, 'cannot be resumed'),
        ):
            with self.subTest(status=status):
                branch = make_branch(f'Describe {status}', status)
                with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
                    response = self.client.get(self._url(branch))
                self.assertEqual(response.status_code, 200)
                content = response.content.decode()
                self.assertIn(expected, content)
                # The status the branch will land in is stated on the page, not just implied.
                target = BranchStatusChoices.RECOVERY_STATUS[status]
                self.assertIn(str(dict(BranchStatusChoices)[target]), content)

    def test_get_offers_a_retry_option_only_where_the_operation_can_be_rerun(self):
        for status, offered in (
            (BranchStatusChoices.SYNCING, True),
            (BranchStatusChoices.MIGRATING, True),
            (BranchStatusChoices.MERGING, False),
            (BranchStatusChoices.REVERTING, False),
            (BranchStatusChoices.PROVISIONING, False),
        ):
            with self.subTest(status=status):
                branch = make_branch(f'Retry offer {status}', status)
                with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
                    response = self.client.get(self._url(branch))
                self.assertEqual(response.status_code, 200)
                self.assertEqual('id_retry' in response.content.decode(), offered)

    def test_post_with_retry_reruns_the_interrupted_operation(self):
        branch = make_branch('Retry Sync', BranchStatusChoices.SYNCING)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING), \
                patch('netbox_branching.jobs.SyncBranchJob.enqueue') as enqueue:
            response = self.client.post(self._url(branch), data={'retry': 'on'})
        self.assertEqual(response.status_code, 302)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)
        enqueue.assert_called_once()
        # The branch must already be out of its transitional status when the retry is queued,
        # otherwise sync() refuses to run.
        self.assertEqual(enqueue.call_args.kwargs['instance'].status, BranchStatusChoices.READY)

    def test_post_without_retry_only_resets_the_status(self):
        branch = make_branch('No Retry', BranchStatusChoices.SYNCING)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING), \
                patch('netbox_branching.jobs.SyncBranchJob.enqueue') as enqueue:
            response = self.client.post(self._url(branch), data={})
        self.assertEqual(response.status_code, 302)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)
        enqueue.assert_not_called()

    def test_post_never_reruns_a_merge(self):
        # A merge writes to main and its dry-run flag dies with the worker, so it is never retried
        # even if the caller asks for it.
        branch = make_branch('No Merge Retry', BranchStatusChoices.MERGING)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING), \
                patch('netbox_branching.jobs.MergeBranchJob.enqueue') as enqueue:
            response = self.client.post(self._url(branch), data={'retry': 'on'})
        self.assertEqual(response.status_code, 302)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)
        enqueue.assert_not_called()

    def test_get_redirects_for_a_non_transitional_branch(self):
        branch = make_branch('Ready View', BranchStatusChoices.READY)
        response = self.client.get(self._url(branch))
        self.assertEqual(response.status_code, 302)

    def test_post_recovers_the_branch(self):
        branch = make_branch('Recover Me', BranchStatusChoices.MIGRATING)
        make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            response = self.client.post(self._url(branch), data={})
        self.assertEqual(response.status_code, 302)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.PENDING_MIGRATIONS)

    def test_post_recovers_a_branch_whose_job_still_looks_alive(self):
        # The operator is asserting that the operation has stopped; the view honours that.
        branch = make_branch('Force Via View', BranchStatusChoices.MERGING)
        make_job(branch, MergeBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            response = self.client.post(self._url(branch), data={})
        self.assertEqual(response.status_code, 302)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)

    def test_post_recovers_without_retrying_by_default(self):
        # Submitting the form is the confirmation; re-running the operation is the opt-in.
        branch = make_branch('Bare Post', BranchStatusChoices.MIGRATING)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING), \
                patch('netbox_branching.jobs.MigrateBranchJob.enqueue') as enqueue:
            response = self.client.post(self._url(branch), data={})
        self.assertEqual(response.status_code, 302)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.PENDING_MIGRATIONS)
        enqueue.assert_not_called()

    def test_get_does_not_preselect_the_retry_option(self):
        branch = make_branch('Retry Default', BranchStatusChoices.SYNCING)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            response = self.client.get(self._url(branch))
        content = response.content.decode()
        retry_input = re.search(r'<input[^>]*id="id_retry"[^>]*>', content).group(0)
        self.assertNotIn('checked', retry_input)

    def test_branch_view_renders_the_stuck_banner(self):
        branch = make_branch('Banner', BranchStatusChoices.MIGRATING)
        make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            response = self.client.get(branch.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('Branch is stuck', content)
        self.assertIn(self._url(branch), content)

    def test_branch_view_omits_the_banner_when_a_job_is_running(self):
        branch = make_branch('No Banner', BranchStatusChoices.MIGRATING)
        make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            response = self.client.get(branch.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Branch is stuck', response.content.decode())

    def test_unprivileged_user_is_denied(self):
        branch = make_branch('Denied', BranchStatusChoices.MIGRATING)
        self.client.force_login(self.unprivileged)
        response = self.client.post(self._url(branch), data={})
        self.assertIn(response.status_code, (302, 403))
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MIGRATING)


@override_settings(PLUGINS_CONFIG=RECOVERY_CONFIG)
class BranchRecoverAPITestCase(BranchConnectionCleanupMixin, TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='recoverapi_super', is_superuser=True)
        self.header = {
            'HTTP_AUTHORIZATION': f'Token {self._create_token(self.user)}',
            'HTTP_ACCEPT': 'application/json',
        }

    # TODO: Remove when dropping support for NetBox v4.4
    @staticmethod
    def _create_token(user):
        try:
            # NetBox >= 4.5
            from users.choices import TokenVersionChoices
            token = Token(version=TokenVersionChoices.V1, user=user)
            token.save()
        except ImportError:
            # NetBox < 4.5
            token = Token(user=user)
            token.save()
            return token.key
        else:
            return token.token

    @staticmethod
    def _url(branch):
        return reverse('plugins-api:netbox_branching-api:branch-recover', kwargs={'pk': branch.pk})

    def test_recover_stuck_branch(self):
        branch = make_branch('API Stuck', BranchStatusChoices.MIGRATING)
        make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING):
            response = self.client.post(self._url(branch), **self.header)
        self.assertEqual(response.status_code, 200)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.PENDING_MIGRATIONS)

    def test_retry_reruns_the_interrupted_operation(self):
        branch = make_branch('API Retry', BranchStatusChoices.MIGRATING)
        make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING), \
                patch('netbox_branching.jobs.MigrateBranchJob.enqueue') as enqueue:
            response = self.client.post(
                self._url(branch), data={'retry': True}, content_type='application/json', **self.header
            )
        self.assertEqual(response.status_code, 200)
        enqueue.assert_called_once()

    def test_retry_defaults_off_over_the_api(self):
        # There is no confirmation step over the API, so re-running is strictly opt-in there.
        branch = make_branch('API No Retry', BranchStatusChoices.MIGRATING)
        make_job(branch, MigrateBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value=RQ_JOB_MISSING), \
                patch('netbox_branching.jobs.MigrateBranchJob.enqueue') as enqueue:
            response = self.client.post(self._url(branch), **self.header)
        self.assertEqual(response.status_code, 200)
        enqueue.assert_not_called()

    def test_non_transitional_branch_is_rejected(self):
        branch = make_branch('API Ready', BranchStatusChoices.READY)
        response = self.client.post(self._url(branch), **self.header)
        self.assertEqual(response.status_code, 400)

    def test_live_job_is_rejected_without_force(self):
        branch = make_branch('API Live', BranchStatusChoices.SYNCING)
        make_job(branch, SyncBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            response = self.client.post(self._url(branch), **self.header)
        self.assertEqual(response.status_code, 400)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.SYNCING)

    def test_live_job_is_recovered_with_force(self):
        branch = make_branch('API Forced', BranchStatusChoices.SYNCING)
        make_job(branch, SyncBranchJob.Meta.name)
        with patch('netbox_branching.utilities._get_rq_job_status', return_value='started'):
            response = self.client.post(
                self._url(branch), data={'force': True}, content_type='application/json', **self.header
            )
        self.assertEqual(response.status_code, 200)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.READY)
