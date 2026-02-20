"""
Merge strategy implementations for branch operations.
"""
from .iterative import IterativeMergeStrategy
from .squash import SquashMergeStrategy
from .strategy import MergeStrategy, get_merge_strategy

__all__ = (
    'IterativeMergeStrategy',
    'MergeStrategy',
    'SquashMergeStrategy',
    'get_merge_strategy',
)
