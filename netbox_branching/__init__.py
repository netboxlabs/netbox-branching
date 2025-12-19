from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from netbox.plugins import PluginConfig, get_plugin_config
from netbox.utils import register_model_feature
from .constants import BRANCH_ACTIONS
from .utilities import supports_branching


class AppConfig(PluginConfig):
    name = 'netbox_branching'
    verbose_name = 'NetBox Branching'
    description = 'A git-like branching implementation for NetBox'
    version = '0.8.0'
    base_url = 'branching'
    min_version = '4.4.1'
    max_version = '4.5.99'
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
        from django.core.signals import request_started, request_finished
        from . import constants, events, search, signal_receivers, webhook_callbacks  # noqa: F401
        from .models import Branch
        from .utilities import DynamicSchemaDict, close_old_branch_connections

        # Validate required settings
        if type(settings.DATABASES) is not DynamicSchemaDict:
            raise ImproperlyConfigured(
                "netbox_branching: DATABASES must be a DynamicSchemaDict instance."
            )
        if 'netbox_branching.database.BranchAwareRouter' not in settings.DATABASE_ROUTERS:
            raise ImproperlyConfigured(
                "netbox_branching: DATABASE_ROUTERS must contain 'netbox_branching.database.BranchAwareRouter'."
            )

        # Register cleanup handler for branch connections (#358)
        # This ensures branch connections are closed when they exceed CONN_MAX_AGE,
        # preventing connection leaks. Django's built-in close_old_connections()
        # only handles connections in DATABASES.keys(), which doesn't include
        # dynamically-created branch aliases.
        request_started.connect(close_old_branch_connections)
        request_finished.connect(close_old_branch_connections)

        # Register the "branching" model feature
        register_model_feature('branching', supports_branching)

        # Validate & register configured branch action validators
        for action in BRANCH_ACTIONS:
            for validator_path in get_plugin_config('netbox_branching', f'{action}_validators'):
                try:
                    func = import_string(validator_path)
                except ImportError:
                    raise ImproperlyConfigured(f"Branch {action} validator not found: {validator_path}")
                Branch.register_preaction_check(func, action)


config = AppConfig
