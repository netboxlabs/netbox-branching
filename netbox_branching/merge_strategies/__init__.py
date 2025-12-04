"""
Merge strategy implementations for branch operations.
"""
from .strategy import MergeStrategy, get_merge_strategy
from .iterative import IterativeMergeStrategy
from .squash import SquashMergeStrategy


__all__ = (
    'MergeStrategy',
    'IterativeMergeStrategy',
    'SquashMergeStrategy',
    'get_merge_strategy',
)
