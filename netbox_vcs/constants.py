# Name of the primary (non-context) PostgreSQL schema
PRIMARY_SCHEMA = 'public'

# Prefix for schema names
SCHEMA_PREFIX = 'ctx_'

# Fields to exclude when calculating object diffs
DIFF_EXCLUDE_FIELDS = ('created', 'last_updated')
