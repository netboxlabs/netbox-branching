import warnings

from netbox.registry import registry

from .contextvars import active_branch


__all__ = (
    'BranchAwareRouter',
)


class BranchAwareRouter:
    """
    A Django database router that returns the appropriate connection/schema for
    the active branch (if any).
    """
    connection_prefix = 'schema_'

    def _get_db(self, model, **hints):
        # Warn & exit if branching support has not yet been initialized
        if 'branching' not in registry['model_features']:
            warnings.warn(f"Routing database query for {model} before branching support is initialized.")
            return

        # Bail if the model does not support branching
        app_label, model_name = model._meta.label.lower().split('.')
        if model_name not in registry['model_features']['branching'].get(app_label, []):
            return

        # Return the schema for the active branch (if any)
        if branch := active_branch.get():
            return f'{self.connection_prefix}{branch.schema_name}'

    def db_for_read(self, model, **hints):
        return self._get_db(model, **hints)

    def db_for_write(self, model, **hints):
        return self._get_db(model, **hints)

    def allow_relation(self, obj1, obj2, **hints):
        # Permit relations from the branch schema to the main schema
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # This router has no opinion on non-branch connections
        if not db.startswith(self.connection_prefix):
            return

        # Disallow migrations for models from the plugin itself within a branch
        if app_label == 'netbox_branching':
            return False

        # Disallow migrations for models which don't support branching
        if model_name and model_name not in registry['model_features']['branching'].get(app_label, []):
            return False
