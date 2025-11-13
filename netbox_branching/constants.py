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

# URL query parameter name
QUERY_PARAM = '_branch'

# Models which do not support change logging, but whose database tables
# must be replicated for each branch to ensure proper functionality
INCLUDE_MODELS = (
    'dcim.cablepath',
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

# Indexes to ignore as they are removed in a NetBox v4.3 migration, but might be present
# in earlier NetBox releases.
# TODO: Remove in v0.6.0
SKIP_INDEXES = (
    'dcim_cabletermination_termination_type_id_termination_id_idx',     # Removed in dcim.0207_remove_redundant_indexes
    'vpn_l2vpntermination_assigned_object_type_id_assigned_objec_idx',  # Removed in vpn.0009_remove_redundant_indexes
    'vpn_tunneltermination_termination_type_id_termination_id_idx',     # Removed in vpn.0009_remove_redundant_indexes
)
