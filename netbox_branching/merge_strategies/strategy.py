import logging

from abc import ABC, abstractmethod
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
        pass

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
        pass

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
