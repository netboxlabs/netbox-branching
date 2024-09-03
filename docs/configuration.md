# Configuration Parameters

## `max_branches`

Default: None

The maximum number of branches that can exist simultaneously, including merged branches that have not been deleted. It may be desirable to limit the total number of provisioned branches to safeguard against excessive database size.

---

## `schema_prefix`

Default: `branch_`

The string to prefix to the unique branch ID when provisioning the PostgreSQL schema for a branch. Per [the PostgreSQL documentation](https://www.postgresql.org/docs/16/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS), this string must begin with a letter or underscore.

Note that a valid prefix is required, as the randomly-generated branch ID alone may begin with a digit, which would not qualify as a valid schema name.
