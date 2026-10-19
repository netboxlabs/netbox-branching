from django.urls import reverse_lazy

try:
    from botocore.exceptions import ClientError as BotocoreClientError
    _FILE_NOT_FOUND_EXCEPTIONS = (FileNotFoundError, BotocoreClientError)
except ImportError:
    _FILE_NOT_FOUND_EXCEPTIONS = (FileNotFoundError,)

__all__ = (
    'BRANCH_ACTIONS',
    'BRANCH_HEADER',
    'COOKIE_NAME',
    'EXEMPT_MODELS',
    'EXEMPT_PATHS',
    'INCLUDE_MODELS',
    'MIGRATE_LOGGER',
    'PG_UNIQUE_VIOLATION',
    'PLUGIN_NAME',
    'PROVISION_LOGGER',
    'QUERY_PARAM',
    'SKIP_INDEXES',
)


# The plugin's name as NetBox knows it: the app's package path, which is what
# AppConfig.name declares and what get_plugin_config() and registry['plugins'] are keyed
# by. It lives here rather than on the AppConfig because the backends need it to read
# their configuration, and importing the package's __init__ from a submodule to reach
# AppConfig.name would make that submodule's import order load-bearing.
PLUGIN_NAME = 'netbox_branching'

# Logging channels for the branch lifecycle operations a backend carries out. They are
# named here because a backend is expected to log into them, which makes them part of the
# backend contract rather than an implementation detail of the one shipped here; see
# BranchingBackend.provision_logger / .migrate_logger.
PROVISION_LOGGER = 'netbox_branching.branch.provision'
MIGRATE_LOGGER = 'netbox_branching.branch.migrate'

# HTTP cookie
COOKIE_NAME = 'active_branch'

# HTTP header for API requests
BRANCH_HEADER = 'X-NetBox-Branch'

# Branch actions
BRANCH_ACTIONS = (
    'sync',
    'merge',
    'migrate',
    'revert',
    'archive',
)

# Paths exempt from branch activation
EXEMPT_PATHS = (
    reverse_lazy('api-status'),
)

# URL query parameter name
QUERY_PARAM = '_branch'

# Models which do not support change logging, but whose database tables
# must be replicated for each branch to ensure proper functionality
INCLUDE_MODELS = (
    'dcim.cablepath',
    'dcim.portmapping',  # Fix for issue #447 - front/rear port mapping table
    'dcim.porttemplatemapping',  # Front/rear port template mapping table (added in NetBox 4.6)
    'extras.cachedvalue',
    'extras.taggeditem',  # Fix for issue #354 - tags through model
    'tenancy.contactgroupmembership',  # Fix for NetBox v4.3.0
)

# Models for which branching support is explicitly disabled
EXEMPT_MODELS = (
    # Exempt applicable core NetBox models
    'core.*',
    'extras.branch',
    'extras.customfield',
    'extras.customfieldchoiceset',
    'extras.customlink',
    'extras.eventrule',
    'extras.exporttemplate',
    'extras.notificationgroup',
    'extras.savedfilter',
    'extras.webhook',

    # Exempt all models from this plugin and from netbox-changes
    'netbox_branching.*',
    'netbox_changes.*',
)

# PostgreSQL error code for unique constraint violations, used in error_report.py
# to detect and generate human-readable messages for duplicate-value merge failures.
PG_UNIQUE_VIOLATION = '23505'

# Indexes to ignore as they are removed in a NetBox v4.3 migration, but might be present
# in earlier NetBox releases.
# TODO: Remove in v0.6.0
SKIP_INDEXES = (
    'dcim_cabletermination_termination_type_id_termination_id_idx',     # Removed in dcim.0207_remove_redundant_indexes
    'vpn_l2vpntermination_assigned_object_type_id_assigned_objec_idx',  # Removed in vpn.0009_remove_redundant_indexes
    'vpn_tunneltermination_termination_type_id_termination_id_idx',     # Removed in vpn.0009_remove_redundant_indexes
)
