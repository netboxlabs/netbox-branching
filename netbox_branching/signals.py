from django.dispatch import Signal

from .events import *

__all__ = (
    'branch_deprovisioned',
    'branch_merged',
    'branch_provisioned',
    'branch_reverted',
    'branch_synced',
)


branch_provisioned = Signal()
branch_deprovisioned = Signal()
branch_synced = Signal()
branch_merged = Signal()
branch_reverted = Signal()

branch_signals = {
    branch_provisioned: BRANCH_PROVISIONED,
    branch_deprovisioned: BRANCH_DEPROVISIONED,
    branch_synced: BRANCH_SYNCED,
    branch_merged: BRANCH_MERGED,
    branch_reverted: BRANCH_REVERTED,
}
