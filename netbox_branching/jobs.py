import logging

from django.db.models.signals import m2m_changed, post_save, pre_delete

from core.choices import JobStatusChoices
from extras.signals import handle_changed_object, handle_deleted_object
from utilities.exceptions import AbortTransaction
from .models import Branch
from .utilities import ListHandler

__all__ = (
    'merge_branch',
    'provision_branch',
    'sync_branch',
)


def get_job_log(job):
    """
    Initialize and return the job log.
    """
    job.data = {
        'log': list()
    }
    return job.data['log']


def provision_branch(job):
    logger = logging.getLogger('netbox_branching.branch.provision')
    logger.setLevel(logging.DEBUG)

    try:
        job.start()
        logger.addHandler(ListHandler(queue=get_job_log(job)))

        # Provision the Branch
        branch = Branch.objects.get(pk=job.object_id)
        branch.provision(job.user)

        job.terminate()

    except Exception as e:
        job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=repr(e))


def sync_branch(job, commit=True):
    job_log = get_job_log(job)
    logger = logging.getLogger('netbox_branching.branch.sync')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(ListHandler(queue=job_log))

    try:
        job.start()

        # Disconnect changelog handlers
        post_save.disconnect(handle_changed_object)
        m2m_changed.disconnect(handle_changed_object)
        pre_delete.disconnect(handle_deleted_object)

        # Sync the Branch
        branch = Branch.objects.get(pk=job.object_id)
        branch.sync(job.user, commit=commit)

        job.terminate()

    except AbortTransaction:
        logger.info("Dry run completed; rolling back changes")
        job.terminate()

    except Exception as e:
        job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=repr(e))

    finally:
        # Reconnect signal handlers
        post_save.connect(handle_changed_object)
        m2m_changed.connect(handle_changed_object)
        pre_delete.connect(handle_deleted_object)


def merge_branch(job, commit=True):
    job_log = get_job_log(job)
    logger = logging.getLogger('netbox_branching.branch.merge')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(ListHandler(queue=job_log))

    try:
        job.start()

        # Merge the Branch
        branch = Branch.objects.get(pk=job.object_id)
        branch.merge(job.user, commit=commit)

        job.terminate()

    except AbortTransaction:
        logger.info("Dry run completed; rolling back changes")
        job.terminate()

    except Exception as e:
        job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=repr(e))
