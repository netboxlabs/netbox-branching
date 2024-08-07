import logging

from django.db.models.signals import m2m_changed, post_save, pre_delete

from core.signals import handle_changed_object, handle_deleted_object
from netbox.jobs import JobRunner
from utilities.exceptions import AbortTransaction
from .utilities import ListHandler

__all__ = (
    'MergeBranchJob',
    'ProvisionBranchJob',
    'RevertBranchJob',
    'SyncBranchJob',
)


def get_job_log(job):
    """
    Initialize and return the job log.
    """
    job.data = {
        'log': list()
    }
    return job.data['log']


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

    def _disconnect_signal_receivers(self):
        """
        Disconnect object change handlers before syncing.
        """
        post_save.disconnect(handle_changed_object)
        m2m_changed.disconnect(handle_changed_object)
        pre_delete.disconnect(handle_deleted_object)

    def _reconnect_signal_receivers(self):
        """
        Reconnect object change handlers after syncing.
        """
        post_save.connect(handle_changed_object)
        m2m_changed.connect(handle_changed_object)
        pre_delete.connect(handle_deleted_object)

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
        except Exception as e:
            # TODO: Can JobRunner be extended to handle this more cleanly?
            # Ensure that signal handlers are reconnected
            self._reconnect_signal_receivers()
            raise e

        # Reconnect signal handlers
        self._reconnect_signal_receivers()


class MergeBranchJob(JobRunner):
    """
    Merge changes from a Branch into main.
    """
    class Meta:
        name = 'Merge branch'

    def run(self, commit=True, *args, **kwargs):
        # Initialize logging
        logger = logging.getLogger('netbox_branching.branch.merge')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(ListHandler(queue=get_job_log(self.job)))

        # Merge the Branch
        try:
            branch = self.job.object
            branch.merge(user=self.job.user, commit=commit)
        except AbortTransaction:
            logger.info("Dry run completed; rolling back changes")


class RevertBranchJob(JobRunner):
    """
    Revert changes from a merged Branch.
    """
    class Meta:
        name = 'Revert branch'

    def run(self, commit=True, *args, **kwargs):
        # Initialize logging
        logger = logging.getLogger('netbox_branching.branch.revert')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(ListHandler(queue=get_job_log(self.job)))

        # Merge the Branch
        try:
            branch = self.job.object
            branch.revert(user=self.job.user, commit=commit)
        except AbortTransaction:
            logger.info("Dry run completed; rolling back changes")
