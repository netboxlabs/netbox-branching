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

See [Configuration: `exempt_models`](configuration.md#exempt_models) for full details.

## Opting In: `register_branching_resolver`

Some plugins have models that are not `ChangeLoggingMixin` subclasses but still need to participate in branching — most commonly **dynamically-generated M2M through tables** that store relationships involving branchable parent objects.

The default branching heuristic excludes any model that does not inherit `ChangeLoggingMixin`, on the assumption that such models are configuration-style records (singletons, choice sets, etc.) that should remain global. For a through table, that assumption is wrong: relationship rows for a branch-only parent must live in the branch schema, not in main, or foreign-key constraints will fail and the relationship will leak across branches.

NetBox Branching ships with a static list (`INCLUDE_MODELS`) covering its own through tables (`extras.taggeditem`, `dcim.portmapping`, etc.). For plugin models — especially when the model name isn't known until runtime — you can register a callable that decides on each query whether a given model should be branchable.

### Resolver Signature

A resolver is a plain function that takes a model class and returns `True`, `False`, or `None`:

```python
def my_resolver(model) -> bool | None:
    ...
```

| Return value | Meaning |
|---|---|
| `True`  | Model is branchable; route queries to the active branch (still subject to the `exempt_models` filter). |
| `False` | Model is not branchable; always route to main. |
| `None`  | Defer to the next resolver, or to the default `ChangeLoggingMixin` heuristic. |

Resolvers are evaluated in registration order. The first non-`None` result wins. Returning `None` for models you don't care about is important — it lets other plugins' resolvers, and the default heuristic, run normally.

### Registration

Register from your `PluginConfig.ready()`. Wrap the import in `try/except ImportError` so your plugin still works when `netbox-branching` is not installed:

```python
# my_plugin/__init__.py
from netbox.plugins import PluginConfig


class MyPluginConfig(PluginConfig):
    name = 'my_plugin'
    # ...

    def ready(self):
        super().ready()
        try:
            from netbox_branching.utilities import register_branching_resolver
            from .branching import my_resolver
            register_branching_resolver(my_resolver)
        except ImportError:
            pass  # netbox-branching not installed; nothing to register
```

`ready()` runs once per worker process at startup, so registration happens exactly once and the resolver list does not need to be deduplicated.

### Example: Dynamically-generated through table

A plugin that creates M2M through tables at runtime — for example `through_my_plugin_<n>_<field>` — can mark them branchable based on a name pattern:

```python
# my_plugin/branching.py

def supports_branching_resolver(model):
    """Mark dynamically-generated M2M through tables as branchable."""
    meta = getattr(model, '_meta', None)
    if meta is None or meta.app_label != 'my_plugin':
        return None
    if (meta.model_name or '').startswith('through_my_plugin_'):
        return True
    return None
```

Registered as above, this routes all matching through-table queries to the active branch's schema. Without it, the through-row INSERT would land in main and fail on the foreign-key constraint to a branch-only parent row.

### When to use it

- A model lacks `ChangeLoggingMixin` but **must** be branchable because it stores relationships or denormalized state for branchable parent objects.
- The model name or app label can be matched dynamically (a name pattern, a class attribute, etc.) and so can't be expressed as a static entry in `INCLUDE_MODELS`.

### When *not* to use it

- The model already inherits `ChangeLoggingMixin`. Branching support is automatic in that case.
- The model is a singleton / configuration record that should remain global. Leave it alone — the default heuristic will keep it in main.
- You only need to bypass branching for a single specific model. Use `exempt_models` instead; it's simpler and more discoverable.

### Interaction with `exempt_models`

A resolver returning `True` does **not** override the `exempt_models` filter. After a resolver opts a model in, `supports_branching` still applies the configured exempt list. So you can use the two together: register a resolver that includes a whole class of plugin models, then exempt specific ones via `PLUGINS_CONFIG`.

## Handling Renamed Fields: `Model.canonicalize_data`

Plugins whose models can be modified at runtime (e.g. user-defined custom object types where individual fields can be added, removed, or renamed) face a subtle problem when their changes are replayed across sync, merge, or revert: a stored `ObjectChange` data dict may carry a field-name key that no longer matches the model's current attribute set.

For example, a custom object type's `description` field may have been renamed to `details_long` in a branch.  The branch's `ObjectChange` records use the new name; main's still use the old name.  When sync replays main's records onto the branch, or merge replays the branch's records onto main, `update_object()` tries to do `instance._meta.get_field(attr)` against the *target* schema's view — which may not recognise the name as it appears in the data dict.

To support this, models may optionally define a `canonicalize_data` classmethod:

```python
class MyModel(NetBoxModel):
    ...

    @classmethod
    def canonicalize_data(cls, data):
        """Rewrite stale field-name keys in `data` to current attribute names."""
        if not data:
            return data
        result = {}
        for raw_key, value in data.items():
            target = _resolve_current_name(cls, raw_key)  # plugin-specific
            result[target] = value
        return result
```

When present, netbox-branching invokes this method in two places:

| Call site | When | Effect |
|---|---|---|
| `update_object()` (`netbox_branching/utilities.py`) | Each UPDATE replay during sync, merge, or revert-undo | The dict passed to the apply loop has keys translated to the model's current attribute names |
| `ChangeDiff._update_conflicts()` (`netbox_branching/models/changes.py`) | Each `ChangeDiff.save()`, triggered by `ObjectChange` post-save | `original`, `modified`, and `current` are each canonicalized before comparison so a rename in one snapshot doesn't appear as a divergent key set |

The hook is **opt-in**.  Models that don't define `canonicalize_data` use the data dict as-is — existing static-model behavior is unchanged.

### When to define `canonicalize_data`

- Your plugin's models have attributes that can be renamed at runtime (e.g. user-controlled schema).
- You're seeing `KeyError` in `ChangeDiff._update_conflicts()` because key sets across `original`/`modified`/`current` diverge for your model.
- `update_object()` is silently dropping writes because `instance._meta.get_field(attr)` raises `FieldDoesNotExist` for stale keys in the data dict.

### When *not* to define it

- Static models — attributes don't change between record-time and apply-time.  The hook is unnecessary overhead.
- Models where every replay path goes through `deserialize_object()` (CREATE actions, DELETE-undo).  Those already have their own model-level hook.

### Hook contract

| Aspect | Contract |
|---|---|
| Receives | A `dict` (the raw data from `ObjectChange` storage or a snapshot field) |
| Returns | A `dict` with keys translated; values unchanged |
| Errors | Propagate normally — a buggy canonicalizer should surface, not silently misroute writes |
| Idempotency | Should be idempotent: calling twice produces the same result as calling once |
| Empty input | Should handle `None` / `{}` gracefully (typically by returning the input unchanged) |

### Collision handling

If two raw keys translate to the same target attribute (e.g. squash-merged data carries both the old name and the new name), the plugin's canonicalizer decides which value wins.  A common convention is "prefer the non-None value", which handles the squash-merge case where `deep_compare_dict` may emit a sentinel `None` under the new name.  Branching does not impose a rule — that's a plugin-level concern.

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

If a plugin is installed or upgraded after branches have been created, the existing branch schemas will **not** receive the new database migrations. Models added or changed by the plugin upgrade will not be fully available in those branches.

The recommended practice is to install or upgrade plugins before creating branches, and to merge or remove all open branches before upgrading a plugin that modifies existing models.
