from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from netbox.plugins import PluginConfig, get_plugin_config
from netbox.registry import registry


class AppConfig(PluginConfig):
    name = 'netbox_branching'
    verbose_name = 'NetBox Branching'
    description = 'A git-like branching implementation for NetBox'
    version = '0.5.1'
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

        # Record all object types which support branching in the NetBox registry
        exempt_models = (
            *constants.EXEMPT_MODELS,
            *get_plugin_config('netbox_branching', 'exempt_models'),
        )
        branching_models = {}
        for app_label, models in registry['model_features']['change_logging'].items():
            # Wildcard exclusion for all models in this app
            if f'{app_label}.*' in exempt_models:
                continue
            models = [
                model for model in models
                if f'{app_label}.{model}' not in exempt_models
            ]
            if models:
                branching_models[app_label] = models
        registry['model_features']['branching'] = branching_models


config = AppConfig
