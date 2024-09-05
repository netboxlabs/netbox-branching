# Name of the main (non-branch) PostgreSQL schema
MAIN_SCHEMA = 'public'

# HTTP cookie
COOKIE_NAME = 'active_branch'

# HTTP header for API requests
BRANCH_HEADER = 'X-NetBox-Branch'

# URL query parameter name
QUERY_PARAM = '_branch'

# Apps which are explicitly excluded from branching
EXCLUDED_APPS = (
    'netbox_branching',
    'netbox_changes',
)
