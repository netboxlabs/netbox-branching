import warnings

from netbox.registry import registry

from .backends import get_branching_backend
from .contextvars import active_branch

__all__ = (
    'BranchAwareRouter',
)


class BranchAwareRouter:
    """
    A Django database router that returns the appropriate connection for the
    active branch (if any). The connection alias and the set of models routed to
    it are both determined by the configured branching backend.
    """

    @property
    def backend(self):
        return get_branching_backend()

    def _get_connection(self, branch):
        return self.backend.get_connection_alias(branch)

    def _get_db(self, model, **hints):
        # Warn & exit if branching support has not yet been initialized
        if 'branching' not in registry['model_features']:
            warnings.warn(f"Routing database query for {model} before branching support is initialized.")
            return None

        # Return the connection for the active branch (if any), provided the backend
        # routes this model to the branch rather than to main
        if branch := active_branch.get():
            if not self.backend.routes_model(model, branch):
                return None
            return self._get_connection(branch)
        return None

    def db_for_read(self, model, **hints):

        # Always use the active branch (if any) when retrieving changelog records
        if model._meta.label == 'core.ObjectChange':
            if branch := active_branch.get():
                return self._get_connection(branch)
            return None

        return self._get_db(model, **hints)

    def db_for_write(self, model, **hints):
        return self._get_db(model, **hints)

    def allow_relation(self, obj1, obj2, **hints):
        # Permit relations from the branch schema to the main schema
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # This router has no opinion on non-branch connections — nor on anything at all when
        # the plugin is disabled, since DATABASE_ROUTERS is host configuration that outlives
        # PLUGINS.
        backend = get_branching_backend(required=False)
        if backend is None or not backend.owns_connection_alias(db):
            return None

        # Which migrations may be applied within a branch depends on what the branch's
        # dataset actually contains, which is the backend's business.
        return backend.allow_migrate(db, app_label, model_name=model_name, **hints)
