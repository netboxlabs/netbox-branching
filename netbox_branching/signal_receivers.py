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
    branch = active_branch.get()
    object_type = instance.changed_object_type
    object_id = instance.changed_object_id

    # If this is a global change, update the "current" state in any ChangeDiffs for this object.
    if branch is None:

        # There cannot be a pre-existing ChangeDiff for an object that was just created.
        if instance.action == ObjectChangeActionChoices.ACTION_CREATE:
            return

        print(f"Updating change diff for global change to {instance.changed_object}")
        if diff := ChangeDiff.objects.filter(object_type=object_type, object_id=object_id, branch__status=BranchStatusChoices.READY).first():
            diff.last_updated = timezone.now()
            diff.current = instance.postchange_data_clean
            diff.save()

    # If this is a branch-aware change, create or update ChangeDiff for this object.
    else:

        # Updating the existing ChangeDiff
        if diff := ChangeDiff.objects.filter(object_type=object_type, object_id=object_id, branch=branch).first():
            print(f"Updating branch change diff for change to {instance.changed_object}")
            diff.last_updated = timezone.now()
            if diff.action != ObjectChangeActionChoices.ACTION_CREATE:
                diff.action = instance.action
            diff.modified = instance.postchange_data_clean
            diff.save()

        # Creating a new ChangeDiff
        else:
            print(f"Creating branch change diff for change to {instance.changed_object}")
            if instance.action == ObjectChangeActionChoices.ACTION_CREATE:
                current_data = None
            else:
                model = instance.changed_object_type.model_class()
                obj = model.objects.using(DEFAULT_DB_ALIAS).get(pk=instance.changed_object_id)
                current_data = serialize_object(obj, exclude=['created', 'last_updated'])
            diff = ChangeDiff(
                branch=branch,
                object_type=instance.changed_object_type,
                object_id=instance.changed_object_id,
                action=instance.action,
                original=instance.prechange_data_clean,
                modified=instance.postchange_data_clean,
                current=current_data,
                last_updated=timezone.now(),
            )
            diff.save()


def handle_branch_event(event_type, branch, user=None, **kwargs):
    """
    Process any EventRules associated with branch events (e.g. syncing or merging).
    """
    # Find any EventRules for this event type
    object_type = ObjectType.objects.get_by_natural_key('netbox_branching', 'branch')
    event_rules = EventRule.objects.filter(
        event_types__contains=[event_type],
        enabled=True,
        object_types=object_type
    )

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


branch_provisioned.connect(partial(handle_branch_event, event_type=BRANCH_PROVISIONED))
branch_synced.connect(partial(handle_branch_event, event_type=BRANCH_SYNCED))
branch_merged.connect(partial(handle_branch_event, event_type=BRANCH_MERGED))
branch_deprovisioned.connect(partial(handle_branch_event, event_type=BRANCH_DEPROVISIONED))


@receiver(pre_delete, sender=Branch)
def validate_branch_deletion(sender, instance, **kwargs):
    """
    Prevent the deletion of a Branch which is in a transitional state (e.g. provisioning, syncing, etc.).
    """
    if instance.status in BranchStatusChoices.TRANSITIONAL:
        raise AbortRequest(
            _("A branch in the {status} status may not be deleted.").format(status=instance.status)
        )
