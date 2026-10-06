"""
Iterative merge strategy implementation.
"""

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS

from core.choices import ObjectChangeActionChoices
from netbox.context_managers import event_tracking

from ..error_report import annotate_validation_error
from .strategy import MergeStrategy

__all__ = ('IterativeMergeStrategy',)


class IterativeMergeStrategy(MergeStrategy):
    """
    Iterative merge strategy that applies/reverts changes one at a time in chronological order.
    """

    def merge(self, branch, changes, request, logger, user):
        """
        Apply changes iteratively in chronological order.
        """
        models = set()
        applied_pks_by_model = {}

        for change in changes:
            model_class = change.changed_object_type.model_class()
            models.add(model_class)
            if change.action != ObjectChangeActionChoices.ACTION_DELETE:
                applied_pks_by_model.setdefault(model_class, []).append(change.changed_object_id)
            with event_tracking(request):
                request.id = change.request_id
                request.user = change.user
                try:
                    change.apply(branch, using=DEFAULT_DB_ALIAS, logger=logger)
                except ValidationError as e:
                    annotate_validation_error(e, model_class, change.changed_object_id, change.changed_object_type_id)
                    raise

        # Run outside event_tracking(): this reconciles derived state, and is not itself a change
        self._update_dependent_objects(applied_pks_by_model, logger)

        self._clean(models)

    def revert(self, branch, changes, request, logger, user):
        """
        Undo changes iteratively (one at a time) in reverse chronological order.
        """
        models = set()
        restored_pks_by_model = {}

        # Undo each change from the Branch
        for change in changes:
            model_class = change.changed_object_type.model_class()
            models.add(model_class)
            if change.action != ObjectChangeActionChoices.ACTION_CREATE:
                restored_pks_by_model.setdefault(model_class, []).append(change.changed_object_id)
            with event_tracking(request):
                request.id = change.request_id
                request.user = change.user
                change.undo(branch, logger=logger)

        # Run outside event_tracking(): this reconciles derived state, and is not itself a change
        self._update_dependent_objects(restored_pks_by_model, logger)

        # Perform cleanup tasks
        self._clean(models)
