import logging
from functools import partial

from django.db import DEFAULT_DB_ALIAS
from django.db.models.signals import post_migrate, post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange, ObjectType
from extras.events import process_event_rules
from extras.models import EventRule
from netbox.signals import post_clean
from netbox_branching import signals
from utilities.exceptions import AbortRequest
from utilities.serialization import serialize_object
from .choices import BranchStatusChoices
from .contextvars import active_branch
from .events import *
from .models import Branch, ChangeDiff
from .utilities import deactivate_branch

__all__ = (
    'check_pending_migrations',
    'handle_branch_event',
    'record_change_diff',
    'validate_branch_deletion',
    'validate_branching_operations',
    'validate_object_deletion_in_branch',
)


@receiver(post_clean)
def validate_branching_operations(sender, instance, **kwargs):
    """
    Validate that branching operations are valid (e.g., not modifying deleted objects).
    """
    branch = active_branch.get()

    # Only validate if we're in a branch and this model supports branching
    if branch is None:
        return

    # Check if this model supports branching
    try:
        object_type = ObjectType.get_for_model(instance.__class__)
        if 'branching' not in object_type.features:
            return
    except ObjectType.DoesNotExist:
        return

    # For updates, check if the object exists in main
    if hasattr(instance, 'pk') and instance.pk is not None:
        model = instance.__class__
        with deactivate_branch():
            try:
                # Try to get the object from main database
                model.objects.get(pk=instance.pk)
            except model.DoesNotExist:
                raise ValidationError(
                    _(
                        "Cannot modify {model_name} '{object_name}' because it has been deleted in the main branch. "
                        "Sync with the main branch to update."
                    ).format(
                        model_name=model._meta.verbose_name,
                        object_name=str(instance)
                    )
                )


@receiver(post_save, sender=ObjectChange)
def record_change_diff(instance, **kwargs):
    """
    When an ObjectChange is created, create or update the relevant ChangeDiff for the active Branch.
    """
    logger = logging.getLogger('netbox_branching.signal_receivers.record_change_diff')

    branch = active_branch.get()
    content_type = instance.changed_object_type
    object_id = instance.changed_object_id

    # If this type of object does not support branching, return immediately.
    if 'branching' not in content_type.object_type.features:
        return

    # If this is a global change, update the "current" state in any ChangeDiffs for this object.
    if branch is None:

        # There cannot be a pre-existing ChangeDiff for an object that was just created.
        if instance.action == ObjectChangeActionChoices.ACTION_CREATE:
            return

        logger.debug(f"Updating change diff for global change to {instance.changed_object}")
        ChangeDiff.objects.filter(
            object_type=content_type,
            object_id=object_id,
            branch__status=BranchStatusChoices.READY
        ).update(
            last_updated=timezone.now(),
            current=instance.postchange_data_clean or None
        )

    # If this is a branch-aware change, create or update ChangeDiff for this object.
    else:

        # Updating the existing ChangeDiff
        if diff := ChangeDiff.objects.filter(object_type=content_type, object_id=object_id, branch=branch).first():
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
                # Check if the object exists in main before trying to get it
                try:
                    model.objects.using(DEFAULT_DB_ALIAS).get(pk=instance.changed_object_id)
                except model.DoesNotExist:
                    raise AbortRequest(
                        _(
                            "Cannot {action} {model_name} '{object_name}' because it has been deleted "
                            "in the main branch. Sync with the main branch to update."
                        ).format(
                            action=instance.action.lower(),
                            model_name=model._meta.verbose_name,
                            object_name=str(instance.changed_object)
                        )
                    )
                with deactivate_branch():
                    obj = model.objects.get(pk=instance.changed_object_id)
                    if hasattr(obj, 'serialize_object'):
                        current_data = obj.serialize_object(exclude=['created', 'last_updated'])
                    else:
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


signals.post_provision.connect(partial(handle_branch_event, event_type=BRANCH_PROVISIONED))
signals.post_deprovision.connect(partial(handle_branch_event, event_type=BRANCH_DEPROVISIONED))
signals.post_sync.connect(partial(handle_branch_event, event_type=BRANCH_SYNCED))
signals.post_merge.connect(partial(handle_branch_event, event_type=BRANCH_MERGED))
signals.post_revert.connect(partial(handle_branch_event, event_type=BRANCH_REVERTED))


@receiver(pre_delete)
def validate_object_deletion_in_branch(sender, instance, **kwargs):
    """
    Validate that objects being deleted in a branch still exist in main.
    """
    # Skip Branch objects - they have their own validation
    if sender == Branch:
        return

    # Only validate if we're in a branch
    branch = active_branch.get()
    if branch is None:
        return

    # Check if this model supports branching
    try:
        object_type = ObjectType.get_for_model(instance.__class__)
        if 'branching' not in object_type.features:
            return
    except ObjectType.DoesNotExist:
        return

    # For deletions, check if the object exists in main
    if hasattr(instance, 'pk') and instance.pk is not None:
        model = instance.__class__
        try:
            # Try to get the object from main database (explicitly use default connection)
            model.objects.using(DEFAULT_DB_ALIAS).get(pk=instance.pk)
        except model.DoesNotExist:
            raise AbortRequest(
                _(
                    "Cannot delete {model_name} '{object_name}' because it has been deleted in the main branch. "
                    "Sync with the main branch to update."
                ).format(
                    model_name=model._meta.verbose_name,
                    object_name=str(instance)
                )
            )


@receiver(pre_delete, sender=Branch)
def validate_branch_deletion(sender, instance, **kwargs):
    """
    Prevent the deletion of a Branch which is in a transitional state (e.g. provisioning, syncing, etc.).
    """
    if instance.status in BranchStatusChoices.TRANSITIONAL:
        raise AbortRequest(
            _("A branch in the {status} status may not be deleted.").format(status=instance.status)
        )


@receiver(post_migrate)
def check_pending_migrations(sender, using, **kwargs):
    """
    Check for any Branches with pending database migrations, and update their status accordingly.
    """
    if sender.name != 'netbox_branching' or using != DEFAULT_DB_ALIAS:
        return
    logger = logging.getLogger('netbox_branching.signal_receivers.check_pending_migrations')
    logger.info("Checking for branches with pending database migrations")

    open_branches = Branch.objects.filter(status=BranchStatusChoices.READY)
    update_count = 0
    for branch in open_branches:
        if branch.pending_migrations:
            branch.status = BranchStatusChoices.PENDING_MIGRATIONS
            update_count += 1
    if update_count:
        logger.info(f"Updating status of {update_count} branches with pending migrations")
        Branch.objects.bulk_update(open_branches, ['status'], batch_size=100)
