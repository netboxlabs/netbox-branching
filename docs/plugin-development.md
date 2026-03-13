# Plugin Development Guide

This guide is for authors of NetBox plugins who want their models to work correctly within branches.

## Model Compatibility

### What Just Works

Any model that inherits from NetBox's `ChangeLoggingMixin` — directly or via a base class — will automatically participate in branching. No additional code is required in your plugin.

This includes models that use any of these base classes from `netbox.models`:

| Base class | Includes change logging |
|---|---|
| `NetBoxModel` | Yes |
| `PrimaryModel` | Yes |
| `OrganizationalModel` | Yes |
| `NestedGroupModel` | Yes |
| `ChangeLoggingMixin` directly | Yes |

Branching works by replaying NetBox's `ObjectChange` audit log. When a user creates, updates, or deletes one of your models inside a branch, NetBox records the change as an `ObjectChange`. The branching plugin's sync and merge machinery then replays those records into main. As long as your models emit `ObjectChange` records (which all `ChangeLoggingMixin`-derived models do automatically), they will be fully supported.

### What Won't Work

Models that do **not** use `ChangeLoggingMixin` are ineligible for branching support and are automatically excluded. This typically includes:

- Configuration-style or singleton models
- Junction/through tables managed entirely by many-to-many fields
- Models you explicitly register as exempt (see below)

These models are still accessible from within a branch, but changes to them made inside a branch are **not isolated** — they affect the main schema immediately, just as if no branch were active.

### Models That Should Not Be Branched

Even if a model uses `ChangeLoggingMixin`, not all models are appropriate candidates for branching. The key question to ask is: _does it make sense to stage changes to this data in isolation before merging it to main?_

Models that represent **network inventory or topology** — devices, sites, prefixes, circuits, and similar — are the primary use case for branching. Models that represent **system-level or administrative state** generally should not be branched. These are records that need to take effect immediately and globally, where isolating changes in a branch would be confusing or counterproductive. Examples include:

- User accounts, API tokens, and permissions
- Plugin configuration or feature-flag style settings
- Schema-defining records (e.g. custom object type definitions) where branching the schema independently of the data it governs would cause inconsistencies

If your plugin includes models in this category that happen to use `ChangeLoggingMixin`, consider registering them in `exempt_models` so they behave as global records regardless of whether a branch is active.

However, there is an important constraint: **any model that has a foreign key or other relationship to a branch-aware model must itself be branch-aware.** You cannot exempt a model that references branchable data, as this would break referential integrity — a record in the global schema pointing at an object that only exists inside a branch, or vice versa.

## Opting Out: `exempt_models`

If your plugin includes models that technically use `ChangeLoggingMixin` but you explicitly don't want branching support for them, use the `exempt_models` configuration setting:

```python
PLUGINS_CONFIG = {
    'netbox_branching': {
        'exempt_models': [
            'my_plugin.mymodel',
        ],
    }
}
```

You can also exempt all models in your plugin at once:

```python
exempt_models = ['my_plugin.*']
```

!!! warning "Relational integrity"
    A model may not be exempted if it has foreign key relationships to models for which branching _is_ supported. Branching support must be consistent across all interrelated models; otherwise, changes inside a branch can corrupt relationships in the main schema. Only exempt models that are fully isolated from branchable models.

See [Configuration: `exempt_models`](configuration.md#exempt_models) for full details.

## Custom Validators

NetBox Branching supports pluggable validator functions that run before each branch action (sync, merge, revert, archive). This allows you or other plugin authors to enforce business rules — for example, preventing a branch from being merged if it has unresolved issues in an external system.

### Validator Signature

A validator is a plain Python callable that accepts a single `Branch` instance and returns a `BranchActionIndicator`:

```python
from netbox_branching.utilities import BranchActionIndicator

def my_merge_validator(branch) -> BranchActionIndicator:
    if some_condition(branch):
        return BranchActionIndicator(permitted=False, message="Cannot merge: reason here.")
    return BranchActionIndicator(permitted=True)
```

`BranchActionIndicator` is a simple dataclass with two fields:

| Field | Type | Description |
|---|---|---|
| `permitted` | `bool` | Whether the action is allowed |
| `message` | `str` | Explanation shown to the user if `permitted=False` |

### Registering Validators via Configuration

The simplest way to register validators is via the plugin configuration. Each action has its own list of validator import paths:

```python
PLUGINS_CONFIG = {
    'netbox_branching': {
        'sync_validators': [
            'my_plugin.validators.require_sync_approval',
        ],
        'merge_validators': [
            'my_plugin.validators.check_external_ticket',
        ],
        'revert_validators': [],
        'archive_validators': [],
    }
}
```

Validators are loaded and registered at startup. If an import path cannot be resolved, NetBox will raise an `ImproperlyConfigured` error on startup.

### Registering Validators Programmatically

You can also register validators from your plugin's `AppConfig.ready()` method using `Branch.register_preaction_check()`:

```python
# my_plugin/__init__.py
from netbox.plugins import PluginConfig

class MyPluginConfig(PluginConfig):
    name = 'my_plugin'
    # ...

    def ready(self):
        super().ready()
        from netbox_branching.models import Branch
        from .validators import check_external_ticket
        Branch.register_preaction_check(check_external_ticket, 'merge')
```

The `action` argument must be one of: `sync`, `merge`, `revert`, `archive`.

!!! note
    Validators registered programmatically are equivalent to those registered via configuration. Both approaches are supported; use whichever fits your plugin's architecture.

## ObjectChange Migrators

When a migration changes the structure of a model — for example, renaming a field, restructuring a relationship, or moving data between fields — any `ObjectChange` records already stored in an open branch will still contain the old data format. If those records are replayed during a merge or sync, they may fail or produce incorrect results because the data no longer matches the current schema.

NetBox addresses this with **objectchange migrators**: a convention where a migration declares a module-level `objectchange_migrators` dictionary mapping model labels to functions that update existing `ObjectChange` records to match the post-migration schema. NetBox Branching reads these migrators from each migration applied to a branch and runs them against the branch's `ObjectChange` records during migration.

If your plugin introduces a migration that changes how a model's data is structured, you should include an `objectchange_migrators` dict in that migration:

```python
def oc_rename_my_field(objectchange, reverting):
    for data in (objectchange.prechange_data, objectchange.postchange_data):
        if data is None:
            continue
        if 'old_field_name' in data:
            data['new_field_name'] = data.pop('old_field_name')


objectchange_migrators = {
    'my_plugin.mymodel': oc_rename_my_field,
}
```

Each migrator function receives:

| Argument | Description |
|---|---|
| `objectchange` | The `ObjectChange` instance being migrated, with `prechange_data` and `postchange_data` JSON fields to update in place |
| `reverting` | `True` if the migration is being reversed; use this to swap the direction of any transformation |

The migrator should modify `prechange_data` and `postchange_data` in place to reflect the new schema. Always guard against `None` values, as one or both fields may be absent depending on whether the change was a create or delete.

## Changelog Considerations

Since branching relies entirely on the `ObjectChange` log, anything that affects how your models serialize or emit changes will also affect how they behave in branches.

- If you override `serialize_object()` on your model, ensure it produces a stable, complete representation — the branch merge machinery uses this data to reconstruct and apply changes.
- Avoid side effects in model `save()` or `delete()` methods that are not captured by `ObjectChange`, as those side effects will not be replayed during a merge.

## Plugin Installation Order

!!! warning
    `netbox_branching` must be listed **last** in the `PLUGINS` configuration. Branching support is only registered for models provided by plugins that appear **before** it in the list.

    ```python
    PLUGINS = [
        'my_plugin',          # branching support registered for my_plugin's models
        'netbox_branching',   # must be last
    ]
    ```

    Any plugin listed after `netbox_branching` will not have its models enrolled in branching support.

## Branches and Plugin Upgrades

If a plugin is installed or upgraded after branches have been created, the existing branch schemas will **not** receive the new database migrations. Models added or changed by the plugin upgrade will not be fully available in those branches.

The recommended practice is to install or upgrade plugins before creating branches, and to merge or remove all open branches before upgrading a plugin that modifies existing models.
