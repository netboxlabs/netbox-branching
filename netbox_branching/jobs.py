import logging
from collections import defaultdict
from contextlib import contextmanager

from core.choices import ObjectChangeActionChoices
from core.signals import handle_changed_object, handle_deleted_object
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Case, IntegerField, Max, When
from django.db.models.signals import m2m_changed, post_save, pre_delete
from netbox.jobs import JobRunner
from netbox.plugins import get_plugin_config
from netbox.signals import post_clean
from utilities.exceptions import AbortTransaction

from .choices import BranchMergeStrategyChoices
from .error_report import build_error_report
from .signal_receivers import validate_branching_operations
from .utilities import ListHandler

__all__ = (
    'MergeBranchJob',
    'MigrateBranchJob',
    'ProvisionBranchJob',
    'RevertBranchJob',
    'SyncBranchJob',
)


def get_job_log(job):
    """
    Initialize and return the job log.
    """
    if job.data is None:
        job.data = {}
    job.data.setdefault('log', [])
    return job.data['log']


@contextmanager
def disconnect_signal_receivers():
    """
    Context manager to temporarily disconnect object change signal handlers. Used during branch
    operations (sync, migrate) that modify the branch schema but should not record ObjectChange
    entries or validate branching constraints.
    """
    post_save.disconnect(handle_changed_object)
    m2m_changed.disconnect(handle_changed_object)
    pre_delete.disconnect(handle_deleted_object)
    post_clean.disconnect(validate_branching_operations)
    try:
        yield
    finally:
        post_save.connect(handle_changed_object)
        m2m_changed.connect(handle_changed_object)
        pre_delete.connect(handle_deleted_object)
        post_clean.connect(validate_branching_operations)


class ProvisionBranchJob(JobRunner):
    """
    Provision a Branch in the database.
    """
    class Meta:
        name = 'Provision branch'

    def run(self, *args, **kwargs):
        # Initialize logging
        logger = logging.getLogger('netbox_branching.branch.provision')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(ListHandler(queue=get_job_log(self.job)))

        # Provision the Branch
        branch = self.job.object
        branch.provision(user=self.job.user)


class SyncBranchJob(JobRunner):
    """
    Sync changes from main into a Branch.
    """
    class Meta:
        name = 'Sync branch'

    @property
    def job_timeout(self):
        """Return the job timeout from plugin configuration."""
        return get_plugin_config('netbox_branching', 'job_timeout')

    def _disconnect_signal_receivers(self):
        """
        Disconnect object change handlers before syncing.
        """
        post_save.disconnect(handle_changed_object)
        m2m_changed.disconnect(handle_changed_object)
        pre_delete.disconnect(handle_deleted_object)
        post_clean.disconnect(validate_branching_operations)

    def _reconnect_signal_receivers(self):
        """
        Reconnect object change handlers after syncing.
        """
        post_save.connect(handle_changed_object)
        m2m_changed.connect(handle_changed_object)
        pre_delete.connect(handle_deleted_object)
        post_clean.connect(validate_branching_operations)

    def run(self, commit=True, *args, **kwargs):
        # Initialize logging
        logger = logging.getLogger('netbox_branching.branch.sync')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(ListHandler(queue=get_job_log(self.job)))

        # Disconnect changelog handlers
        self._disconnect_signal_receivers()

        # Sync the branch
        try:
            branch = self.job.object
            branch.sync(user=self.job.user, commit=commit)
        except AbortTransaction:
            logger.info("Dry run completed; rolling back changes")
        except Exception:
            # TODO: Can JobRunner be extended to handle this more cleanly?
            # Ensure that signal handlers are reconnected
            self._reconnect_signal_receivers()
            raise

        # Reconnect signal handlers
        self._reconnect_signal_receivers()


class MergeBranchJob(JobRunner):
    """
    Merge changes from a Branch into main.
    """
    class Meta:
        name = 'Merge branch'

    @property
    def job_timeout(self):
        """Return the job timeout from plugin configuration."""
        return get_plugin_config('netbox_branching', 'job_timeout')

    @staticmethod
    def _snapshot_changes_summary(changes_qs):
        """
        Compute a JSON-serializable changes summary from a queryset, snapshotted at job start time.
        Stored as {'creates': {'app_label.model': count}, ...} so it remains accurate even if the
        branch state changes later. Uses DB-level aggregation to avoid loading all changes into memory.
        """
        ACTION_CREATE = ObjectChangeActionChoices.ACTION_CREATE
        ACTION_DELETE = ObjectChangeActionChoices.ACTION_DELETE

        # Aggregate per unique object at the DB level — one row per object, not per change record
        per_object = (
            changes_qs
            .values('changed_object_type_id', 'changed_object_id')
            .annotate(
                has_delete=Max(Case(
                    When(action=ACTION_DELETE, then=1), default=0, output_field=IntegerField()
                )),
                has_create=Max(Case(
                    When(action=ACTION_CREATE, then=1), default=0, output_field=IntegerField()
                )),
            )
        )

        creates = defaultdict(int)
        updates = defaultdict(int)
        deletes = defaultdict(int)

        for obj in per_object:
            ct_id = obj['changed_object_type_id']
            if obj['has_delete']:
                deletes[ct_id] += 1
            elif obj['has_create']:
                creates[ct_id] += 1
            else:
                updates[ct_id] += 1

        def resolve(counts_dict):
            result = {}
            for ct_id, count in counts_dict.items():
                try:
                    ct = ContentType.objects.get_for_id(ct_id)
                    result[f'{ct.app_label}.{ct.model}'] = count
                except ContentType.DoesNotExist:
                    pass
            return result

        return {
            'creates': resolve(creates),
            'creates_total': sum(creates.values()),
            'updates': resolve(updates),
            'updates_total': sum(updates.values()),
            'deletes': resolve(deletes),
            'deletes_total': sum(deletes.values()),
        }

    def run(self, commit=True, *args, **kwargs):
        # Initialize logging
        logger = logging.getLogger('netbox_branching.branch.merge')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(ListHandler(queue=get_job_log(self.job)))

        # Snapshot pending changes before merging
        branch = self.job.object
        self.job.data['report'] = []
        self.job.data['changes_summary'] = self._snapshot_changes_summary(branch.get_unmerged_changes())
        self.job.data['has_unsynced_changes'] = branch.get_unsynced_changes().exists()
        self.job.data['merge_strategy'] = branch.merge_strategy or BranchMergeStrategyChoices.ITERATIVE

        # Merge the Branch
        try:
            branch.merge(user=self.job.user, commit=commit)
        except AbortTransaction:
            logger.info("Dry run completed; rolling back changes")
        except (IntegrityError, ValidationError) as e:
            self.job.data['report'].append(build_error_report(e))
            raise


class RevertBranchJob(JobRunner):
    """
    Revert changes from a merged Branch.
    """
    class Meta:
        name = 'Revert branch'

    @property
    def job_timeout(self):
        """Return the job timeout from plugin configuration."""
        return get_plugin_config('netbox_branching', 'job_timeout')

    def run(self, commit=True, *args, **kwargs):
        # Initialize logging
        logger = logging.getLogger('netbox_branching.branch.revert')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(ListHandler(queue=get_job_log(self.job)))

        # Revert the Branch
        try:
            branch = self.job.object
            branch.revert(user=self.job.user, commit=commit)
        except AbortTransaction:
            logger.info("Dry run completed; rolling back changes")


class MigrateBranchJob(JobRunner):
    """
    Apply any outstanding database migrations from the main schema to the Branch.
    """
    class Meta:
        name = 'Migrate branch'

    def run(self, *args, **kwargs):
        # Initialize logging
        logger = logging.getLogger('netbox_branching.branch.migrate')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(ListHandler(queue=get_job_log(self.job)))

        # Disconnect changelog handlers during migration to prevent data migrations
        # from creating spurious ObjectChange records in the branch schema (#542)
        with disconnect_signal_receivers():
            try:
                branch = self.job.object
                branch.migrate(user=self.job.user)
            except AbortTransaction:
                logger.info("Dry run completed; rolling back changes")
