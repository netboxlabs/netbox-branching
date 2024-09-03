from netbox.plugins import PluginConfig
from netbox.registry import registry


class AppConfig(PluginConfig):
    name = 'netbox_branching'
    verbose_name = 'NetBox Branching'
    description = 'A git-like branching implementation for NetBox'
    version = '0.3.1'
    base_url = 'branching'
    min_version = '4.1'
    middleware = [
        'netbox_branching.middleware.BranchMiddleware'
    ]
    default_settings = {
        # The maximum number of branches which can be provisioned simultaneously
        'max_branches': None,

        # This string is prefixed to the name of each new branch schema during provisioning
        'schema_prefix': 'branch_',
    }

    def ready(self):
        super().ready()
        from . import constants, events, search, signal_receivers

        # Record all object types which support branching in the NetBox registry
        if 'branching' not in registry['model_features']:
            registry['model_features']['branching'] = {
                k: v for k, v in registry['model_features']['change_logging'].items()
                if k not in constants.EXCLUDED_APPS
            }


config = AppConfig
