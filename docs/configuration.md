# Configuration Parameters

This page documents the configuration parameters specific to the NetBox Branching plugin. They are set under the `netbox_branching` key of NetBox's `PLUGINS_CONFIG` dictionary, for example:

```python
PLUGINS_CONFIG = {
    'netbox_branching': {
        'max_working_branches': 10,
        'stale_warning_threshold': 14,
    },
}
```

A small number of related settings live outside the plugin's own configuration; those are covered in the [NetBox settings](#netbox-settings) section at the bottom of this page.

---

## `archive_validators`

Default: `[]` (empty list)

A list of import paths to functions which validate whether a branch is permitted to be archived. See [Plugin Development: Custom Validators](./plugin-development.md#custom-validators) for the validator signature and usage details.

---

## `exempt_models`

Default: `[]` (empty list)

A list of models provided by other plugins which should be exempted from branching support. (Only models which support change logging are eligible for branching in the first place; non-change-logged models are excluded automatically.)

!!! warning
    A model may not be exempted from branching support if it has one or more relationships to models for which branching _is_ supported. Branching **must** be supported consistently for all inter-related models; otherwise, data corruption can occur. Configure this setting only if you have a specific need to disable branching for certain models provided by plugins.

Models must be specified by app label and model name:

```python
exempt_models = [
    'my_plugin.foo',
    'my_plugin.bar',
]
```

To exclude _all_ models from within a plugin, substitute an asterisk (`*`) for the model name:

```python
exempt_models = [
    'my_plugin.*',
]
```

See [Plugin Development: Opting Out](./plugin-development.md#opting-out-exempt_models) for guidance on when a plugin author should request that one of their own models be exempted.

---

## `job_timeout`

Default: `3600` (1 hour)

The maximum time in seconds that long-running branch operations (sync, merge, revert) are allowed to execute before timing out. This timeout applies to the background jobs that process branch changes.

For installations with very large branches that may take longer than one hour to sync or merge, this value should be increased accordingly. Note that, as with any branching tool, the general recommendation is to keep branches as short-lived as possible.

```python
PLUGINS_CONFIG = {
    'netbox_branching': {
        'job_timeout': 7200,  # 2 hours
    }
}
```

---

## `main_schema`

Default: `"public"`

The name of the main (primary) PostgreSQL schema. (Use the `\dn` command in the PostgreSQL CLI to list all schemas.)

---

## `max_branches`

Default: `None`

The maximum total number of branches that can exist simultaneously, including merged branches that have not been archived or deleted. It may be desirable to limit the total number of provisioned branches to safeguard against excessive database size. A value of `None` (the default) imposes no limit.

---

## `max_working_branches`

Default: `None`

The maximum number of working (i.e. non-merged, non-archived) branches that can exist simultaneously. A value of `None` (the default) imposes no limit.

---

## `merge_validators`

Default: `[]` (empty list)

A list of import paths to functions which validate whether a branch is permitted to be merged. See [Plugin Development: Custom Validators](./plugin-development.md#custom-validators) for the validator signature and usage details.

---

## `migrate_validators`

Default: `[]` (empty list)

A list of import paths to functions which validate whether a branch is permitted to be migrated. See [Plugin Development: Custom Validators](./plugin-development.md#custom-validators) for the validator signature and usage details.

---

## `revert_validators`

Default: `[]` (empty list)

A list of import paths to functions which validate whether a branch is permitted to be reverted. See [Plugin Development: Custom Validators](./plugin-development.md#custom-validators) for the validator signature and usage details.

---

## `schema_prefix`

Default: `"branch_"`

The string to prefix to the unique branch ID when provisioning the PostgreSQL schema for a branch. Per [the PostgreSQL documentation](https://www.postgresql.org/docs/16/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS), this string must begin with a letter or underscore.

A non-empty prefix is required, because the randomly-generated branch ID alone may begin with a digit, which is not a valid PostgreSQL schema name.

---

## `stale_warning_threshold`

Default: `7`

The number of days before a branch becomes stale at which a warning is displayed on the branch detail page. Set to `0` to disable the warning entirely. A branch becomes stale (and can no longer be synced) once its `last_sync` time exceeds NetBox's configured [`CHANGELOG_RETENTION`](https://netboxlabs.com/docs/netbox/en/stable/configuration/miscellaneous/#changelog_retention) window.

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

A list of import paths to functions which validate whether a branch is permitted to be synced. See [Plugin Development: Custom Validators](./plugin-development.md#custom-validators) for the validator signature and usage details.

---

## NetBox Settings

The settings below are not part of the plugin's own `PLUGINS_CONFIG` block, but interact with the plugin and may need to be updated in `configuration.py`.

### `EVENTS_PIPELINE`

To include branch context in event rule processing, add the plugin's `add_branch_context` function to NetBox's [`EVENTS_PIPELINE`](https://netboxlabs.com/docs/netbox/en/stable/configuration/miscellaneous/#events_pipeline) setting **before** `extras.events.process_event_queue`:

```python
EVENTS_PIPELINE = [
    'netbox_branching.events.add_branch_context',
    'extras.events.process_event_queue',
]
```

When active, this injects an `active_branch` key into each queued event's data payload, with `id`, `name`, and `schema_id` fields (or `null` if the change was made on main). See [Event Rules](./event-rules.md) for usage details.

!!! note
    This entry must be placed **before** `extras.events.process_event_queue` in the list to take effect.
