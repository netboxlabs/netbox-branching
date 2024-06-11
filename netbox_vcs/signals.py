from django.db import DEFAULT_DB_ALIAS
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from extras.choices import ObjectChangeActionChoices
from extras.models import ObjectChange
from utilities.serialization import serialize_object

from .contextvars import active_context
from .models import ChangeDiff


@receiver(post_save, sender=ObjectChange)
def record_change_diff(instance, **kwargs):
    """
    When an ObjectChange is created, create or update the relevant ChangeDiff for the active Context.
    """
    context = active_context.get()
    object_type = instance.changed_object_type
    object_id = instance.changed_object_id

    # If this is a global change, update the "current" state in any ChangeDiffs for this object.
    if context is None:

        # There cannot be a pre-existing ChangeDiff for an object that was just created.
        if instance.action == ObjectChangeActionChoices.ACTION_CREATE:
            return

        print(f"Updating change diff for global change to {instance.changed_object}")
        if diff := ChangeDiff.objects.filter(object_type=object_type, object_id=object_id).first():
            diff.last_updated = timezone.now()
            diff.current = instance.postchange_data
            diff.save()

    # If this is a context-aware change, create or update ChangeDiff for this object.
    else:

        # Updating the existing ChangeDiff
        if diff := ChangeDiff.objects.filter(object_type=object_type, object_id=object_id, context=context).first():
            print(f"Updating context change diff for change to {instance.changed_object}")
            diff.last_updated = timezone.now()
            if diff.action != ObjectChangeActionChoices.ACTION_CREATE:
                diff.action = instance.action
            diff.modified = instance.postchange_data
            diff.save()

        # Creating a new ChangeDiff
        else:
            print(f"Creating context change diff for change to {instance.changed_object}")
            if instance.action == ObjectChangeActionChoices.ACTION_CREATE:
                current_data = None
            else:
                model = instance.changed_object_type.model_class()
                obj = model.objects.using(DEFAULT_DB_ALIAS).get(pk=instance.changed_object_id)
                current_data = serialize_object(obj)
            diff = ChangeDiff(
                context=context,
                object_type=instance.changed_object_type,
                object_id=instance.changed_object_id,
                action=instance.action,
                original=instance.prechange_data,
                modified=instance.postchange_data,
                current=current_data,
                last_updated=timezone.now(),
            )
            diff.save()
