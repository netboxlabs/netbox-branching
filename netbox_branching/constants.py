# Name of the main (non-branch) PostgreSQL schema
MAIN_SCHEMA = 'public'

# HTTP cookie
COOKIE_NAME = 'active_branch'

# HTTP header for API requests
BRANCH_HEADER = 'X-NetBox-Branch'

# URL query parameter name
QUERY_PARAM = '_branch'

# Models which do not support change logging, but whose database tables
# must be replicated for each branch to ensure proper functionality
INCLUDE_MODELS = (
    'dcim.cablepath',
    'extras.cachedvalue',
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
