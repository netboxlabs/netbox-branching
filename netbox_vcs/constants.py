# Name of the primary (non-context) PostgreSQL schema
PRIMARY_SCHEMA = 'public'

# Prefix for schema names
SCHEMA_PREFIX = 'ctx_'

# Fields to exclude when calculating object diffs
DIFF_EXCLUDE_FIELDS = ('created', 'last_updated')

# HTTP cookie
COOKIE_NAME = 'active_context'

# HTTP header for API requests
CONTEXT_HEADER = 'X-NetBox-Context'

# URL query parameter name
QUERY_PARAM = '_context'
