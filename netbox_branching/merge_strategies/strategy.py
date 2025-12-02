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

    @abstractmethod
    def merge(self, branch, changes, request, commit, logger):
        """
        Merge changes from the branch into the main schema.

        Args:
            branch: The Branch instance being merged
            changes: QuerySet of ObjectChanges to merge
            request: Django request object for event tracking
            commit: Boolean indicating whether to commit changes
            logger: Logger instance for logging
        """
        pass

    @abstractmethod
    def revert(self, branch, changes, request, commit, logger):
        """
        Revert changes that were previously merged.

        Args:
            branch: The Branch instance being reverted
            changes: QuerySet of ObjectChanges to revert
            request: Django request object for event tracking
            commit: Boolean indicating whether to commit changes
            logger: Logger instance for logging
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
    Get the appropriate merge strategy instance based on the strategy name.

    Args:
        strategy_name: String name of the strategy from BranchMergeStrategyChoices

    Returns:
        MergeStrategy instance
    """
    from netbox_branching.choices import BranchMergeStrategyChoices
    from .iterative import IterativeMergeStrategy
    from .squash import SquashMergeStrategy

    if strategy_name == BranchMergeStrategyChoices.SQUASH:
        return SquashMergeStrategy()
    else:
        return IterativeMergeStrategy()
