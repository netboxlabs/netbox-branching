import logging
import uuid

from django.db.models.signals import m2m_changed, post_save, pre_delete
from django.test import RequestFactory
from django.urls import reverse

from core.choices import JobStatusChoices
from extras.context_managers import event_tracking
from extras.signals import handle_changed_object, handle_deleted_object
from rq.timeouts import JobTimeoutException
from utilities.exceptions import AbortTransaction

from .choices import ContextStatusChoices
from .models import Context

__all__ = (
    'apply_context',
    'provision_context',
    'rebase_context',
)


logger = logging.getLogger(__name__)


def provision_context(job):
    context = Context.objects.get(pk=job.object_id)

    try:
        job.start()
        logger.info(f"Provisioning context {context} ({context.schema_name})")
        context.provision()
        job.terminate()

    except Exception as e:
        job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=repr(e))
        Context.objects.filter(pk=context.pk).update(status=ContextStatusChoices.FAILED)
        if type(e) is JobTimeoutException:
            logging.error(e)
        else:
            raise e


def apply_context(job, commit=True, request_id=None):
    context = Context.objects.get(pk=job.object_id)

    # Create a dummy request for the event_tracking() context manager
    request = RequestFactory().get(reverse('home'))
    request.id = request_id or uuid.uuid4()
    request.user = job.user

    try:
        job.start()
        logger.info(f"Applying context {context} ({context.schema_name})")
        with event_tracking(request):
            context.apply(commit=commit)
        job.terminate()

    except AbortTransaction:
        job.terminate(status=JobStatusChoices.STATUS_COMPLETED)
        Context.objects.filter(pk=context.pk).update(status=ContextStatusChoices.READY)

    except Exception as e:
        job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=repr(e))
        Context.objects.filter(pk=context.pk).update(status=ContextStatusChoices.FAILED)
        if type(e) is JobTimeoutException:
            logging.error(e)
        else:
            raise e


def rebase_context(job, commit=True):
    context = Context.objects.get(pk=job.object_id)

    try:
        job.start()
        logger.info(f"Rebasing context {context} ({context.schema_name})")

        # Disconnect changelog handlers
        post_save.disconnect(handle_changed_object)
        m2m_changed.disconnect(handle_changed_object)
        pre_delete.disconnect(handle_deleted_object)

        context.rebase(commit=commit)

        job.terminate()

    except AbortTransaction:
        job.terminate(status=JobStatusChoices.STATUS_COMPLETED)
        Context.objects.filter(pk=context.pk).update(status=ContextStatusChoices.READY)

    except Exception as e:
        job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=repr(e))
        Context.objects.filter(pk=context.pk).update(status=ContextStatusChoices.FAILED)
        logging.error(e)

    finally:
        # Reconnect signal handlers
        post_save.connect(handle_changed_object)
        m2m_changed.connect(handle_changed_object)
        pre_delete.connect(handle_deleted_object)
