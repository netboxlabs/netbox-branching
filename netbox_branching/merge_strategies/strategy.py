import logging
from abc import ABC, abstractmethod

from django.db import DEFAULT_DB_ALIAS
from mptt.models import MPTTModel

__all__ = (
    'MergeStrategy',
    'get_merge_strategy',
)


class MergeStrategy(ABC):
    """
    Abstract base class for merge strategies.
    """
    # Ordering for changes queryset in revert() operation
    # Merge always uses chronological order ('time')
    revert_changes_ordering = '-time'  # Reverse chronological order (newest first)

    @abstractmethod
    def merge(self, branch, changes, request, logger, user):
        """
        Merge changes from the branch into the main schema.

        Args:
            branch: The Branch instance being merged
            changes: QuerySet of ObjectChanges to merge
            request: Django request object for event tracking
            logger: Logger instance for logging
            user: User who initiated the merge
        """

    @abstractmethod
    def revert(self, branch, changes, request, logger, user):
        """
        Revert changes that were previously merged.

        Args:
            branch: The Branch instance being reverted
            changes: QuerySet of ObjectChanges to revert
            request: Django request object for event tracking
            logger: Logger instance for logging
            user: User who initiated the revert
        """

    def _update_dependent_objects(self, pks_by_model, logger):
        """
        Update the objects which depend on those just applied (e.g. the CablePaths traversing a Cable).
        Applying a create writes the object with a raw save, which bypasses Model.save() and the dependent
        objects it maintains; models expose the work through update_dependent_objects(). (#469)

        Must be called only once every change has been applied: retracing a Cable, for instance, requires
        its CableTerminations to exist.

        Args:
            pks_by_model: Mapping of model classes to the PKs of the objects applied for each
            logger: Logger instance for logging
        """
        for model, pks in pks_by_model.items():
            if not hasattr(model, 'update_dependent_objects'):
                continue
            queryset = model.objects.using(DEFAULT_DB_ALIAS).filter(pk__in=pks)
            for instance in queryset.iterator(chunk_size=100):
                logger.debug(f"Updating objects dependent on {model._meta.verbose_name} {instance.pk}")
                instance.update_dependent_objects()

    def _clean(self, models):
        """
        Called after syncing, merging, or reverting a branch.
        """
        logger = logging.getLogger('netbox_branching.branch')

        for model in models:

            # Recalculate MPTT as needed
            if issubclass(model, MPTTModel):
                logger.debug(f"Recalculating MPTT for model {model}")
                model.objects.rebuild()


def get_merge_strategy(strategy_name):
    """
    Get the appropriate merge strategy class based on the strategy name.

    Args:
        strategy_name: String name of the strategy from BranchMergeStrategyChoices, or None

    Returns:
        MergeStrategy class (caller should instantiate)

    Raises:
        ValueError: If the strategy name is unknown
    """
    from netbox_branching.choices import BranchMergeStrategyChoices

    from .iterative import IterativeMergeStrategy
    from .squash import SquashMergeStrategy

    # Default to ITERATIVE if strategy_name is None
    if strategy_name is None:
        strategy_name = BranchMergeStrategyChoices.ITERATIVE

    strategies = {
        BranchMergeStrategyChoices.SQUASH: SquashMergeStrategy,
        BranchMergeStrategyChoices.ITERATIVE: IterativeMergeStrategy,
    }

    try:
        return strategies[strategy_name]
    except KeyError as exc:
        raise ValueError(f"Invalid strategy name: {strategy_name}") from exc
