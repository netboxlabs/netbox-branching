"""
Iterative merge strategy implementation.
"""
from django.db import DEFAULT_DB_ALIAS

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

        for change in changes:
            models.add(change.changed_object_type.model_class())
            with event_tracking(request):
                request.id = change.request_id
                request.user = change.user
                change.apply(branch, using=DEFAULT_DB_ALIAS, logger=logger)

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
