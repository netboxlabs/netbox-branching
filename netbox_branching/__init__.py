from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from netbox.plugins import PluginConfig
from .utilities import register_models


class AppConfig(PluginConfig):
    name = 'netbox_branching'
    verbose_name = 'NetBox Branching'
    description = 'A git-like branching implementation for NetBox'
    version = '0.5.2'
    base_url = 'branching'
    min_version = '4.1'
    middleware = [
        'netbox_branching.middleware.BranchMiddleware'
    ]
    default_settings = {
        # The maximum number of working branches (excludes merged & archived branches)
        'max_working_branches': None,

        # The maximum number of branches which can be provisioned simultaneously
        'max_branches': None,

        # Models from other plugins which should be excluded from branching support
        'exempt_models': [],

        # This string is prefixed to the name of each new branch schema during provisioning
        'schema_prefix': 'branch_',
    }

    def ready(self):
        super().ready()
        from . import constants, events, search, signal_receivers
        from .utilities import DynamicSchemaDict

        # Validate required settings
        if type(settings.DATABASES) is not DynamicSchemaDict:
            raise ImproperlyConfigured(
                "netbox_branching: DATABASES must be a DynamicSchemaDict instance."
            )
        if 'netbox_branching.database.BranchAwareRouter' not in settings.DATABASE_ROUTERS:
            raise ImproperlyConfigured(
                "netbox_branching: DATABASE_ROUTERS must contain 'netbox_branching.database.BranchAwareRouter'."
            )

        # Register models which support branching
        register_models()


config = AppConfig
