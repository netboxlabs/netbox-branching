"""
Iterative merge strategy implementation.
"""
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

        for change in changes:
            model_class = change.changed_object_type.model_class()
            models.add(model_class)
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
