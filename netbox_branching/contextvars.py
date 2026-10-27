from contextvars import ContextVar

__all__ = ('active_branch', 'branch_refusal')


active_branch = ContextVar('active_branch', default=None)

# Why BranchAwareRouter refuses every branchable query instead of routing it. Set when a background
# job's request names a branch which cannot be activated: nothing refuses the job as BranchMiddleware
# would an HTTP request, so the first query fails it rather than writing to main.
branch_refusal = ContextVar('branch_refusal', default=None)
