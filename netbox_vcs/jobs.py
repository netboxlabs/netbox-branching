import logging

from core.choices import JobStatusChoices
from rq.timeouts import JobTimeoutException

from .choices import ContextStatusChoices
from .models import Context

__all__ = (
    'provision_context',
)


logger = logging.getLogger(__name__)


def provision_context(job, *args, **kwargs):
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
