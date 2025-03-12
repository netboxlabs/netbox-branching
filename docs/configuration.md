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

## `job_timeout`

Default: 300

The maximum execution time of a background task.  Sync, Merge and Revert jobs will also be given additional modifiers below multiplied by the count of changes in the branch.

## `job_timeout_modifier`

Default: 
```
{
    "default_create": 1,  # seconds
    "default_update": .3,  # seconds
    "default_delete": 1,  # seconds
},
```

This will add additional job timeout padding into the `job_timeout` based on the count of objects changed in a branch.  You can also individually set model's time padding based on your own database performance.

### Example for padding DCIM Devices
```
{
    "default_create": 1,  # seconds
    "default_update": .3,  # seconds
    "default_delete": 1,  # seconds
    "dcim.device": {
        "create": 2,  # seconds
        "update": 1,  # seconds
        "delete": 2,  # seconds
    }
},
```

## `job_timeout_warning`

Default: 900

This will display a warning if the active branch or viewing branch details when the job timeout (plus padding) exceeds this set value.  The warning can be suppressed if set to `None`


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
