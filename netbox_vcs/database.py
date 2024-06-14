from .contextvars import active_branch


__all__ = (
    'BranchAwareRouter',
)


class BranchAwareRouter:
    """
    A Django database router that returns the appropriate connection/schema for
    the active branch (if any).
    """
    def db_for_read(self, model, **hints):
        if branch := active_branch.get():
            return f'schema_{branch.schema_name}'
        return None

    def db_for_write(self, model, **hints):
        if branch := active_branch.get():
            return f'schema_{branch.schema_name}'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        # Permit relations from the branch schema to the main (public) schema
        return True
