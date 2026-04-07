"""
Iterative merge strategy implementation.
"""
from core.choices import ObjectChangeActionChoices
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS
from netbox.context_managers import event_tracking

from ..error_report import annotate_validation_error
from .strategy import MergeStrategy

__all__ = (
    'IterativeMergeStrategy',
)


class IterativeMergeStrategy(MergeStrategy):
    """
    Iterative merge strategy that applies/reverts changes one at a time in chronological order.
    """

    def merge(self, branch, changes, request, logger, user):
        """
        Apply changes iteratively in chronological order.
        """
        models = set()

        # Track (model, pk) pairs for objects whose CREATE was skipped, so that any subsequent
        # UPDATE/DELETE changes for the same object can also be skipped safely.
        skipped_objects = set()

        for change in changes:
            model_class = change.changed_object_type.model_class()
            models.add(model_class)
            obj_key = (model_class, change.changed_object_id)

            if change.action == ObjectChangeActionChoices.ACTION_CREATE:
                # If the object no longer exists in the branch schema it was cascade-deleted during
                # a sync (e.g. its FK parent was deleted in main). Skip and record so that any
                # later UPDATE/DELETE changes for the same object are also skipped.
                if not model_class.objects.using(branch.connection_name).filter(pk=change.changed_object_id).exists():
                    logger.info(
                        f'Skipping CREATE for {model_class._meta.verbose_name} ID {change.changed_object_id} '
                        f'(object no longer exists in branch)'
                    )
                    skipped_objects.add(obj_key)
                    continue

            elif obj_key in skipped_objects:
                # A previous CREATE for this object was skipped; skip subsequent changes too.
                logger.debug(
                    f'Skipping {change.get_action_display()} for {model_class._meta.verbose_name} '
                    f'ID {change.changed_object_id} (CREATE was skipped)'
                )
                continue

            with event_tracking(request):
                request.id = change.request_id
                request.user = change.user
                try:
                    change.apply(branch, using=DEFAULT_DB_ALIAS, logger=logger)
                except ValidationError as e:
                    annotate_validation_error(e, model_class, change.changed_object_id, change.changed_object_type_id)
                    raise

        self._clean(models)

    def revert(self, branch, changes, request, logger, user):
        """
        Undo changes iteratively (one at a time) in reverse chronological order.
        """
        models = set()

        # Undo each change from the Branch
        for change in changes:
            models.add(change.changed_object_type.model_class())
            with event_tracking(request):
                request.id = change.request_id
                request.user = change.user
                change.undo(branch, logger=logger)

        # Perform cleanup tasks
        self._clean(models)
