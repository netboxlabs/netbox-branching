"""
Iterative merge strategy implementation.
"""
from core.choices import ObjectChangeActionChoices
from django.db import DEFAULT_DB_ALIAS, models
from netbox.context_managers import event_tracking

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

        # Pre-scan: collect all (model, pk) pairs being created in this merge so we can distinguish
        # between FK parents that will be created here vs. parents already missing from main.
        objects_being_created = {
            (c.changed_object_type.model_class(), c.changed_object_id)
            for c in changes
            if c.action == ObjectChangeActionChoices.ACTION_CREATE
        }

        for change in changes:
            model = change.changed_object_type.model_class()
            models.add(model)
            obj_key = (model, change.changed_object_id)

            if change.action == ObjectChangeActionChoices.ACTION_CREATE:
                # If the object no longer exists in the branch schema it was cascade-deleted during
                # a sync (e.g. its FK parent was deleted in main). Skip and record so that any
                # later UPDATE/DELETE changes for the same object are also skipped.
                if not model.objects.using(branch.connection_name).filter(pk=change.changed_object_id).exists():
                    logger.debug(
                        f'Skipping CREATE for {model._meta.verbose_name} ID {change.changed_object_id} '
                        f'(object no longer exists in branch)'
                    )
                    skipped_objects.add(obj_key)
                    continue

                # If a FK parent was deleted from main without a sync, the object still exists in
                # the branch but cannot be created in main. Also cascade-skip if a parent's CREATE
                # was itself skipped.
                missing_parent = self._get_missing_fk_parent(
                    model, change.postchange_data, objects_being_created, skipped_objects
                )
                if missing_parent:
                    logger.debug(
                        f'Skipping CREATE for {model._meta.verbose_name} ID {change.changed_object_id} '
                        f'(FK parent {missing_parent} missing from main)'
                    )
                    skipped_objects.add(obj_key)
                    continue

            elif obj_key in skipped_objects:
                # A previous CREATE for this object was skipped; skip subsequent changes too.
                logger.debug(
                    f'Skipping {change.get_action_display()} for {model._meta.verbose_name} '
                    f'ID {change.changed_object_id} (CREATE was skipped)'
                )
                continue

            with event_tracking(request):
                request.id = change.request_id
                request.user = change.user
                change.apply(branch, using=DEFAULT_DB_ALIAS, logger=logger)

        self._clean(models)

    @staticmethod
    def _get_missing_fk_parent(model_class, postchange_data, objects_being_created, skipped_objects):
        """
        Check whether any required FK parent for a CREATE is unavailable in main. Returns a string
        description of the first missing parent found, or None if all parents are present.

        A parent is considered missing if it:
        - Is not being created as part of this merge, or its CREATE was skipped, AND
        - Does not exist in main (DEFAULT_DB_ALIAS)
        """
        if not postchange_data:
            return None
        for field in model_class._meta.get_fields():
            if not isinstance(field, models.ForeignKey):
                continue
            fk_value = postchange_data.get(field.name)
            if not fk_value:
                continue
            parent_key = (field.related_model, fk_value)
            # If the parent is being created by this merge and wasn't skipped, it will exist in main.
            if parent_key in objects_being_created and parent_key not in skipped_objects:
                continue
            if not field.related_model.objects.using(DEFAULT_DB_ALIAS).filter(pk=fk_value).exists():
                return f'{field.related_model._meta.verbose_name} ID {fk_value}'
        return None

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
