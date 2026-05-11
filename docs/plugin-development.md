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

**Multi-table inheritance is not supported.** Models that use Django's [multi-table inheritance](https://docs.djangoproject.com/en/6.0/topics/db/models/#multi-table-inheritance) are not compatible with NetBox Branching. Each model in a branch must map to a single, self-contained table. Attempting to provision a branch when such models are present will result in a provisioning error.

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

See [Configuration: `exempt_models`](./configuration.md#exempt_models) for full details.

## Custom Validators

NetBox Branching supports pluggable validator functions that run before each branch action (`sync`, `merge`, `migrate`, `revert`, `archive`). This allows you or other plugin authors to enforce business rules — for example, preventing a branch from being merged if it has unresolved issues in an external system.

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
        'migrate_validators': [],
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

The `action` argument must be one of `sync`, `merge`, `migrate`, `revert`, or `archive`.

!!! note
    Validators registered programmatically are equivalent to those registered via configuration. Both approaches are supported; use whichever fits your plugin's architecture.

## Lifecycle Signals

The plugin exposes pre- and post-event Django signals for every branch lifecycle operation. These provide a low-friction integration point for plugins that need to react to branch state changes — for example, to update an external ticketing system, refresh a cache, or audit who merged what.

The following signals are defined in `netbox_branching.signals`:

| Operation     | Pre-event signal   | Post-event signal   |
|---------------|--------------------|---------------------|
| Provisioning  | `pre_provision`    | `post_provision`    |
| Deprovisioning| `pre_deprovision`  | `post_deprovision`  |
| Syncing       | `pre_sync`         | `post_sync`         |
| Migrating     | `pre_migrate`      | `post_migrate`      |
| Merging       | `pre_merge`        | `post_merge`        |
| Reverting     | `pre_revert`       | `post_revert`       |

Each signal is sent with `sender=Branch`, the affected `branch` instance, and (where applicable) the `user` who initiated the action. Connect to them as you would any other Django signal:

```python
from django.dispatch import receiver
from netbox_branching.models import Branch
from netbox_branching.signals import post_merge

@receiver(post_merge, sender=Branch)
def on_branch_merged(sender, branch, user, **kwargs):
    # Notify an external system, refresh a cache, etc.
    ...
```

## Changelog Considerations

Since branching relies entirely on the `ObjectChange` log, anything that affects how your models serialize or emit changes will also affect how they behave in branches.

- If you override `serialize_object()` on your model, ensure it produces a stable, complete representation — the branch merge machinery uses this data to reconstruct and apply changes.
- Avoid side effects in model `save()` or `delete()` methods that are not captured by `ObjectChange`, as those side effects will not be replayed during a merge.

## Database Migrations

When a branch is migrated, NetBox Branching applies the same migration plan that's been applied to main, but it **fakes** (marks applied without running) any migration whose model-specific operations affect only non-branchable models. This prevents `RunSQL` and `RunPython` operations from inadvertently acting on the main schema via PostgreSQL's `search_path`.

The heuristic can't always determine intent. A migration with no model-specific operations — for example, a pure `RunPython` data backfill — runs on the branch by default, because the framework can't introspect what the function does. If your migration shouldn't run on branches (or should run when the heuristic would skip it), declare `fake_on_branch` at the top of the migration module:

```python
# my_plugin/migrations/0010_backfill_something.py
from django.db import migrations

# Skip this migration on branch schemas; only run it on main
fake_on_branch = True


def backfill(apps, schema_editor):
    ...


class Migration(migrations.Migration):
    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
```

`fake_on_branch` accepts three states:

| Value | Behavior |
|---|---|
| `True` | Always fake on branches |
| `False` | Always run on branches (overrides the heuristic) |
| Not set | Apply the default heuristic |

### When to set `fake_on_branch = True`

Use this when a `RunPython` or `RunSQL` operation only makes sense against the main schema — for example, backfilling data on an exempt model, performing one-off cross-schema queries, or migrating system-level state (users, tokens, configuration). The migration will still be marked applied on the branch, so its dependency chain remains intact.

### When to set `fake_on_branch = False`

Use this only when the default heuristic would incorrectly fake a migration that needs to run on branches. This is uncommon but can happen if a `RunPython` that operates on branchable data sits in the same migration as a non-branchable schema operation — in that case the heuristic would fake the whole migration based on the schema op alone.

### When to leave it unset

Pure schema migrations (`AddField`, `AlterField`, etc.) on branchable models don't need the flag — the heuristic handles them correctly by running them on every branch.

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

If a plugin is installed or upgraded after branches have been created, the existing branch schemas will **not** automatically receive the new database migrations. Branches with outstanding migrations will be flagged with the **Pending Migrations** status and can be brought up to date using the **Migrate** action; until they are migrated, they cannot be activated or merged.

The recommended practice is to install or upgrade plugins before creating branches, and to merge or remove all open branches before upgrading a plugin that modifies existing models.
