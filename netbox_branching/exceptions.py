__all__ = (
    'BranchNotReady',
)


class BranchNotReady(Exception):
    """
    Raised when a request references a Branch which is not ready for use.
    """
    def __init__(self, branch):
        self.branch = branch
        super().__init__(f"Branch {branch} is not ready for use (status: {branch.status})")
