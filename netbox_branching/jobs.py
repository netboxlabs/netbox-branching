import logging

from django.db.models.signals import m2m_changed, post_save, pre_delete

from core.choices import JobStatusChoices
from extras.signals import handle_changed_object, handle_deleted_object
from utilities.exceptions import AbortTransaction
from .models import Branch

__all__ = (
    'merge_branch',
    'provision_branch',
    'sync_branch',
)


logger = logging.getLogger(__name__)


def provision_branch(job):
    branch = Branch.objects.get(pk=job.object_id)

    try:
        job.start()
        logger.info(f"Provisioning branch {branch} ({branch.schema_name})")
        branch.provision()
        job.terminate()

    except Exception as e:
        job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=repr(e))
        logging.error(e)


def sync_branch(job, commit=True):
    branch = Branch.objects.get(pk=job.object_id)

    try:
        # Disconnect changelog handlers
        post_save.disconnect(handle_changed_object)
        m2m_changed.disconnect(handle_changed_object)
        pre_delete.disconnect(handle_deleted_object)

        job.start()
        logger.info(f"Syncing branch {branch} ({branch.schema_id})")
        branch.sync(commit=commit)
        job.terminate()

    except AbortTransaction:
        job.terminate()

    except Exception as e:
        job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=repr(e))
        logging.error(e)

    finally:
        # Reconnect signal handlers
        post_save.connect(handle_changed_object)
        m2m_changed.connect(handle_changed_object)
        pre_delete.connect(handle_deleted_object)


def merge_branch(job, commit=True):
    branch = Branch.objects.get(pk=job.object_id)

    try:
        job.start()
        logger.info(f"Merging branch {branch} ({branch.schema_id})")
        branch.merge(job.user, commit=commit)
        job.terminate()

    except AbortTransaction:
        job.terminate()

    except Exception as e:
        job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=repr(e))
        logging.error(e)
