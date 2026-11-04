from contextvars import ContextVar

__all__ = ('active_branch', 'branch_refusal')


active_branch = ContextVar('active_branch', default=None)

# When set, BranchAwareRouter refuses branchable queries (a job's branch could not be activated)
branch_refusal = ContextVar('branch_refusal', default=None)
