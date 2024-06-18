from django.db import DEFAULT_DB_ALIAS
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange
from utilities.serialization import serialize_object
from .choices import BranchStatusChoices
from .contextvars import active_branch
from .models import AppliedChange, ChangeDiff

__all__ = (
    'record_applied_change',
    'record_change_diff',
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


def record_applied_change(instance, branch, **kwargs):
    """
    Create a new AppliedChange instance mapping an applied ObjectChange to its Branch.
    """
    AppliedChange.objects.create(change=instance, branch=branch)
