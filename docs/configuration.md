# Configuration Parameters

## NetBox `EVENTS_PIPELINE` (required for branch context in event processing)

To include branch context in event rule processing, add the plugin's `add_branch_context` function to NetBox's [`EVENTS_PIPELINE`](https://netboxlabs.com/docs/netbox/en/stable/configuration/miscellaneous/#events_pipeline) setting **before** `extras.events.process_event_queue`:

```python
EVENTS_PIPELINE = [
    'netbox_branching.events.add_branch_context',
    'extras.events.process_event_queue',
]
```

When active, this injects an `active_branch` key into each queued event's data payload with `id`, `name`, and `schema_id` fields (or `null` if the change was made on main). See [Event Rules](event-rules.md) for usage details.

!!! note
    This must be placed **before** `extras.events.process_event_queue` in the list to take effect.

---

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

## `main_schema`

Default: `"public"`

The name of the main (primary) PostgreSQL schema. (Use the `\dn` command in the PostgreSQL CLI to list all schemas.)

---

## `max_working_branches`

Default: None

The maximum number of operational branches that can exist simultaneously. This count excludes branches which have been merged or archived.

---

## `max_branches`

Default: None

The maximum total number of branches that can exist simultaneously, including merged branches that have not been deleted. It may be desirable to limit the total number of provisioned branches to safeguard against excessive database size.

---

## `job_timeout`

Default: `3600` (1 hour)

The maximum time in seconds that long-running branch operations (sync, merge, revert) are allowed to execute before timing out. This timeout applies to background jobs that process large branches.

For installations with very large branches that may take longer than one hour to sync or merge, this value should be increased accordingly. Note: As with any tool that offers branching, the general recommendation is to keep branches as short lived as possible. 

```python
PLUGINS_CONFIG = {
    'netbox_branching': {
        'job_timeout': 7200,  # 2 hours
    }
}
```

---

## `schema_prefix`

Default: `"branch_"`

The string to prefix to the unique branch ID when provisioning the PostgreSQL schema for a branch. Per [the PostgreSQL documentation](https://www.postgresql.org/docs/16/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS), this string must begin with a letter or underscore.

Note that a valid prefix is required, as the randomly-generated branch ID alone may begin with a digit, which would not qualify as a valid schema name.

---

## `stale_warning_threshold`

Default: `7`

The number of days before a branch becomes stale at which a warning is displayed on the branch detail page. Set to `0` to disable the warning entirely.

For example, if `CHANGELOG_RETENTION` is set to 30 days and `stale_warning_threshold` is set to 7, the warning will appear when a branch has not been synced within the last 23 days (i.e. 7 or fewer days remain before the branch becomes stale).

```python
PLUGINS_CONFIG = {
    'netbox_branching': {
        'stale_warning_threshold': 14,
    }
}
```

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
