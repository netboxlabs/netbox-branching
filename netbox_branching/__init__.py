from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from netbox.plugins import PluginConfig, get_plugin_config
from .constants import BRANCH_ACTIONS
from .utilities import register_models


class AppConfig(PluginConfig):
    name = 'netbox_branching'
    verbose_name = 'NetBox Branching'
    description = 'A git-like branching implementation for NetBox'
    version = '0.6.1'
    base_url = 'branching'
    min_version = '4.3.2'
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

        # The name of the main schema
        'main_schema': 'public',

        # This string is prefixed to the name of each new branch schema during provisioning
        'schema_prefix': 'branch_',

        # Branch action validators
        'sync_validators': [],
        'merge_validators': [],
        'migrate_validators': [],
        'revert_validators': [],
        'archive_validators': [],
    }

    def ready(self):
        super().ready()
        from . import constants, events, search, signal_receivers  # noqa: F401
        from .models import Branch
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

        # Validate & register configured branch action validators
        for action in BRANCH_ACTIONS:
            for validator_path in get_plugin_config('netbox_branching', f'{action}_validators'):
                try:
                    func = import_string(validator_path)
                except ImportError:
                    raise ImproperlyConfigured(f"Branch {action} validator not found: {validator_path}")
                Branch.register_preaction_check(func, action)


config = AppConfig
