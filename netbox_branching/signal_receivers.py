import logging
from functools import partial

from django.db import DEFAULT_DB_ALIAS
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange, ObjectType
from extras.events import process_event_rules
from extras.models import EventRule
from netbox.registry import registry
from utilities.exceptions import AbortRequest
from utilities.serialization import serialize_object
from .choices import BranchStatusChoices
from .contextvars import active_branch
from .events import *
from .models import Branch, ChangeDiff
from .signals import *

__all__ = (
    'handle_branch_event',
    'record_change_diff',
    'validate_branch_deletion',
)


@receiver(post_save, sender=ObjectChange)
def record_change_diff(instance, **kwargs):
    """
    When an ObjectChange is created, create or update the relevant ChangeDiff for the active Branch.
    """
    logger = logging.getLogger('netbox_branching.signal_receivers.record_change_diff')

    branch = active_branch.get()
    object_type = instance.changed_object_type
    object_id = instance.changed_object_id

    # If this type of object does not support branching, return immediately.
    if object_type.model not in registry['model_features']['branching'].get(object_type.app_label, []):
        return

    # If this is a global change, update the "current" state in any ChangeDiffs for this object.
    if branch is None:

        # There cannot be a pre-existing ChangeDiff for an object that was just created.
        if instance.action == ObjectChangeActionChoices.ACTION_CREATE:
            return

        logger.debug(f"Updating change diff for global change to {instance.changed_object}")
        ChangeDiff.objects.filter(
            object_type=object_type,
            object_id=object_id,
            branch__status=BranchStatusChoices.READY
        ).update(
            last_updated=timezone.now(),
            current=instance.postchange_data_clean or None
        )

    # If this is a branch-aware change, create or update ChangeDiff for this object.
    else:

        # Updating the existing ChangeDiff
        if diff := ChangeDiff.objects.filter(object_type=object_type, object_id=object_id, branch=branch).first():
            logger.debug(f"Updating branch change diff for change to {instance.changed_object}")
            diff.last_updated = timezone.now()
            if diff.action != ObjectChangeActionChoices.ACTION_CREATE:
                diff.action = instance.action
            diff.modified = instance.postchange_data_clean or None
            diff.save()

        # Creating a new ChangeDiff
        else:
            logger.debug(f"Creating branch change diff for change to {instance.changed_object}")
            if instance.action == ObjectChangeActionChoices.ACTION_CREATE:
                current_data = None
            else:
                model = instance.changed_object_type.model_class()
                obj = model.objects.using(DEFAULT_DB_ALIAS).get(pk=instance.changed_object_id)
                current_data = serialize_object(obj, exclude=['created', 'last_updated'])
            diff = ChangeDiff(
                branch=branch,
                object=instance.changed_object,
                action=instance.action,
                original=instance.prechange_data_clean or None,
                modified=instance.postchange_data_clean or None,
                current=current_data or None,
                last_updated=timezone.now(),
            )
            diff.save()


def handle_branch_event(event_type, branch, user=None, **kwargs):
    """
    Process any EventRules associated with branch events (e.g. syncing or merging).
    """
    logger = logging.getLogger('netbox_branching.signal_receivers.handle_branch_event')
    logger.debug(f"Checking for {event_type} event rules")

    # Find any EventRules for this event type
    object_type = ObjectType.objects.get_by_natural_key('netbox_branching', 'branch')
    event_rules = EventRule.objects.filter(
        event_types__contains=[event_type],
        enabled=True,
        object_types=object_type
    )
    if not event_rules:
        logger.debug("No matching event rules found")
        return
    logger.debug(f"Found {len(event_rules)} event rules")

    # Serialize the branch & process EventRules
    username = user.username if user else None
    data = serialize_object(branch)
    data['id'] = branch.pk
    process_event_rules(
        event_rules=event_rules,
        object_type=object_type,
        event_type=event_type,
        data=data,
        username=username
    )


post_provision.connect(partial(handle_branch_event, event_type=BRANCH_PROVISIONED))
post_deprovision.connect(partial(handle_branch_event, event_type=BRANCH_DEPROVISIONED))
post_sync.connect(partial(handle_branch_event, event_type=BRANCH_SYNCED))
post_merge.connect(partial(handle_branch_event, event_type=BRANCH_MERGED))
post_revert.connect(partial(handle_branch_event, event_type=BRANCH_REVERTED))


@receiver(pre_delete, sender=Branch)
def validate_branch_deletion(sender, instance, **kwargs):
    """
    Prevent the deletion of a Branch which is in a transitional state (e.g. provisioning, syncing, etc.).
    """
    if instance.status in BranchStatusChoices.TRANSITIONAL:
        raise AbortRequest(
            _("A branch in the {status} status may not be deleted.").format(status=instance.status)
        )
