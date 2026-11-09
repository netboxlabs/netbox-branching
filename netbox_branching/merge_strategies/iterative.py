"""
Iterative merge strategy implementation.
"""

from itertools import groupby
from operator import attrgetter

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS

from core.choices import ObjectChangeActionChoices
from netbox.context_managers import event_tracking

from ..error_report import annotate_validation_error
from .squash import SquashMergeStrategy
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
        for change in self._order_for_revert(changes):
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

    @staticmethod
    def _order_for_revert(changes):
        """
        Yield changes newest first, except that within one request an UPDATE whose prechange data references an
        object the request deleted earlier (e.g. an Interface left by a Cable delete) waits for its restore. (#693)
        """
        for _, group in groupby(changes, key=attrgetter('request_id')):
            group = list(group)
            pending = {_change_key(c) for c in group if c.action == ObjectChangeActionChoices.ACTION_DELETE}
            waiting = []

            for change in group:
                if change.action == ObjectChangeActionChoices.ACTION_UPDATE and pending:
                    model_class = change.changed_object_type.model_class()
                    refs = SquashMergeStrategy._get_fk_references(model_class, change.prechange_data, pending)
                    if refs:
                        waiting.append((change, refs))
                        continue

                yield change

                if change.action == ObjectChangeActionChoices.ACTION_DELETE:
                    pending.discard(_change_key(change))
                    released = [c for c, refs in waiting if not refs & pending]
                    waiting = [(c, refs) for c, refs in waiting if refs & pending]
                    yield from released

            yield from (c for c, _ in waiting)


def _change_key(change):
    app_label, model = change.changed_object_type.natural_key()
    return f'{app_label}.{model}', change.changed_object_id
