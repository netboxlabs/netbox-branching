from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from netbox.plugins import PluginConfig
from .utilities import register_models


class AppConfig(PluginConfig):
    name = 'netbox_branching'
    verbose_name = 'NetBox Branching'
    description = 'A git-like branching implementation for NetBox'
    version = '0.5.3'
    base_url = 'branching'
    min_version = '4.1.9'
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

        # The maximum execution time of a background task.
        'job_timeout': 5 * 60,  # seconds

        # This will add additional job timeout padding into the `job_timeout`
        # based on the count of objects changed in a branch.
        'job_timeout_modifier': {
            "default_create": 1,  # seconds
            "default_update": .3,  # seconds
            "default_delete": 1,  # seconds
        },

        # This will display a warning if the active branch or viewing branch
        # details when the job timeout (plus padding) exceeds this set value.
        'job_timeout_warning': 15 * 60,  # seconds
    }

    def ready(self):
        super().ready()
        from . import constants, events, search, signal_receivers  # noqa: F401
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
