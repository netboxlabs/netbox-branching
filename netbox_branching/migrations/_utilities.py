"""
Helpers for the plugin's data migrations.

The leading underscore keeps Django's migration loader from treating this module as a
migration.
"""
from netbox.plugins import get_plugin_config

__all__ = (
    'get_schema_config',
)

PLUGIN_NAME = 'netbox_branching'

# The schema configuration parameters read by data migrations, with their defaults as of
# the migrations which read them.
SCHEMA_CONFIG_DEFAULTS = {
    'main_schema': 'public',
    'schema_prefix': 'branch_',
}


def get_schema_config(name):
    """
    Return the value of the schema-based branching configuration parameter ``name``.

    Every migration calling this predates pluggable backends, so a branch it operates on
    can only have been provisioned as a PostgreSQL schema. The parameter is read from
    ``backend_config``, where it now belongs; failing that, from the root of the plugin's
    configuration, where v1.x installs set it; and failing that, from its historical
    default.
    """
    backend_config = get_plugin_config(PLUGIN_NAME, 'backend_config') or {}
    if name in backend_config:
        return backend_config[name]
    return get_plugin_config(PLUGIN_NAME, name, SCHEMA_CONFIG_DEFAULTS[name])
