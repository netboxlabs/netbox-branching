from contextvars import ContextVar

__all__ = (
    'active_context',
)


active_context = ContextVar('active_context', default=None)
