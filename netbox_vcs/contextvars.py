from contextvars import ContextVar

__all__ = (
    'active_branch',
)


active_branch = ContextVar('active_branch', default=None)
