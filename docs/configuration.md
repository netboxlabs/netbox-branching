# Configuration Parameters

## `exempt_models`

Default: `[]` (empty list)

A list of models provided by other plugins which should be exempt from branching support. (Only models which support change logging need be listed; all other models are ineligible for branching support.)

!!! warning
    A model may not be exempted from branching support if it has one or more relationships to models for which branching is supported. Branching **must** be supported consistently for all inter-related models; otherwise, data corruption can occur. Configure this setting only if you have a specific need to disable branching for certain models provided by plugins.

Models must be specified by app label and model name, as such:

```python
exempt_models = (
    'my_plugin.foo',
    'my_plugin.bar',
)
```

It is also possible to exclude _all_ models from within a plugin by substituting an asterisk (`*`) for the model name:

```python
exempt_models = (
    'my_plugin.*',
)
```

---

## `max_working_branches`

Default: None

The maximum number of operational branches that can exist simultaneously. This count excludes branches which have been merged or archived.

---

## `max_branches`

Default: None

The maximum total number of branches that can exist simultaneously, including merged branches that have not been deleted. It may be desirable to limit the total number of provisioned branches to safeguard against excessive database size.

---

## `schema_prefix`

Default: `branch_`

The string to prefix to the unique branch ID when provisioning the PostgreSQL schema for a branch. Per [the PostgreSQL documentation](https://www.postgresql.org/docs/16/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS), this string must begin with a letter or underscore.

Note that a valid prefix is required, as the randomly-generated branch ID alone may begin with a digit, which would not qualify as a valid schema name.

---

## `sync_validators`

Default: `[]` (empty list)

A list of import paths to functions which validate whether a branch is permitted to be synced.

---

## `merge_validators`

Default: `[]` (empty list)

A list of import paths to functions which validate whether a branch is permitted to be merged.

---

## `revert_validators`

Default: `[]` (empty list)

A list of import paths to functions which validate whether a branch is permitted to be reverted.

---

## `archive_validators`

Default: `[]` (empty list)

A list of import paths to functions which validate whether a branch is permitted to be archived.
