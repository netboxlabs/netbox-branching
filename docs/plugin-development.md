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
- Schema-defining records where branching the schema independently of the data it governs would cause inconsistencies

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
- If your plugin mutates branchable objects outside of NetBox's standard views/viewsets (e.g. in a background job or signal receiver), call `obj.snapshot()` before saving. See [Programmatic Modifications](./best-practices.md#programmatic-modifications-scripts-shells-and-custom-code) in the Best Practices guide for why this matters in a branch context.

## Database Migrations

When a branch is migrated, NetBox Branching applies the same migration plan that's been applied to main, but it **fakes** (marks applied without running) any migration whose model-specific operations affect only non-branchable models. This prevents `RunSQL` and `RunPython` operations from inadvertently acting on the main schema via PostgreSQL's `search_path`.

!!! note
    Faking is a behaviour of the default [`SchemaBranchingBackend`](#branching-backends), because it exists specifically to keep `RunSQL` bodies from reaching the main schema through the branch connection's `search_path`. `fake_on_branch` therefore has no effect under a backend that does not share a `search_path` with main.

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

## Branching Backends

A **branching backend** owns the mechanism of branch isolation: how a branch's isolated dataset is created and destroyed, how the database connections addressing it are named and configured, and how outstanding Django migrations are applied to it. Everything layered above that — change tracking, conflict detection, merge strategies, branch status transitions, signals and events — is backend-agnostic.

The plugin ships one backend, `netbox_branching.backends.SchemaBranchingBackend`, which replicates the main schema into a dedicated PostgreSQL schema per branch. It is selected by default and is what every existing installation uses. The seam exists so that an alternative isolation mechanism (for example, a storage-level copy-on-write clone) can be substituted without touching the machinery built on top of it.

!!! warning
    This is an advanced extension point. Writing a backend means taking responsibility for the invariants below; getting one wrong typically manifests only when a branch is merged, not when it is created.

### The Contract

Subclass `netbox_branching.backends.BranchingBackend` and implement the abstract methods:

| Method | Responsibility |
|---|---|
| `provision(branch, user)` | Assign the branch an identifier (see below), then make the isolated dataset exist, or raise. On failure, clean up your own partial state before raising. |
| `deprovision(branch)` | Destroy the isolated dataset. Must be safe to call for a branch that was never successfully provisioned, including one with no `backend_id`. |
| `get_connection_alias(branch)` | Return the Django connection alias addressing the branch. Must begin with the backend's `connection_alias_prefix`. |
| `get_connection_config(alias, default_config)` | Return the `DATABASES` entry for `alias`, or `None` if the alias isn't yours. |

You must also declare a `connection_alias_prefix`: a non-empty string, unique to your backend, identifying the connection aliases it owns. There is no default — a backend which leaves it unset is rejected at startup — because silently inheriting another backend's prefix would have `owns_connection_alias()` claim that backend's aliases, including any left behind by a previous install.

The rest have useful defaults and only need overriding for specific needs:

| Method | Default |
|---|---|
| `owns_connection_alias(alias)` | Prefix check against `connection_alias_prefix` |
| `connection_alias(suffix)` | `connection_alias_prefix` + `suffix`; raises `ValueError` for an empty suffix |
| `connection_alias_suffix(alias)` | The part of `alias` after the prefix, or `None` if it isn't yours |
| `routes_model(model, branch)` | `supports_branching(model)` |
| `allow_migrate(db, app_label, model_name=None, **hints)` | Refuses the plugin's own models and every non-branchable model; permits `core.ObjectChange` |
| `get_pending_migrations(branch)` | Asks Django's migration executor on the branch's connection what it would still apply |
| `apply_migrations(branch, progress_callback=None)` | Builds the executor, short-circuits an empty plan, and runs it under `activate_branch()` |
| `run_migration_plan(branch, executor, targets, plan)` | `executor.migrate(targets)` |
| `validate_configuration()` | Asserts the host requirements below; **call `super()`** if you add your own |
| `get_detail_fields(branch)` | No rows; override to add `(label, value)` rows to the branch detail page |
| `objectchange_table` | The name of NetBox's change log table (property) |
| `get_branch_tables()` | Every branchable model's table, plus the change log |
| `provision_logger` / `migrate_logger` | The `netbox_branching.branch.provision` / `.migrate` channels (properties) |

`validate_configuration()` is called once from `AppConfig.ready()`, and its default asserts the
two things the plugin's own routing machinery needs from the host configuration, whatever backend
is in use: `DATABASES` wrapped in a `DynamicSchemaDict` (without it `get_connection_config()` is
never consulted and no branch connection can exist) and `BranchAwareRouter` in `DATABASE_ROUTERS`
(without it `get_connection_alias()` is never called and every branch-aware query silently
addresses main). It also rejects any parameter set under `backend_config` that the configured backend does not
declare in `default_config`, so an operator's typo fails at boot rather than resolving silently to
the default. A backend that validates its own parameters should do so *after* calling
`super().validate_configuration()`.

`apply_migrations()` is a template method, and what it does around the plan is contract rather
than mechanism: it resolves the branch's connection, builds a `MigrationExecutor` carrying the
progress callback, returns early when there is nothing to apply, and runs the plan inside
`activate_branch()`. That last part is not optional — without it, ORM queries inside a `RunPython`
data migration fall through `BranchAwareRouter` to the default connection and read main, which may
already have been migrated past the columns the branch's pending migration still depends on. To
apply the plan differently, override **`run_migration_plan()`**, which is called with the branch
already active. The default is a plain `executor.migrate(targets)`, which is correct when a branch
is a self-contained database; `SchemaBranchingBackend` overrides it because its branch shares a
database with main, so a `RunSQL` body can reach main's schema through the connection's
`search_path`.

`connection_alias()` and `connection_alias_suffix()` are inverses, and using them rather than
interpolating `connection_alias_prefix` by hand is what keeps the aliases a backend builds while
provisioning or tearing a branch down identical to the ones it hands the router. The suffix is
whatever your backend addresses a branch by — usually its `backend_id`, though
`SchemaBranchingBackend` uses the schema name derived from it.

Two class attributes carry a backend's own configuration — `default_config` and
`legacy_config_params` — and are covered under [Configuration](#configuration) below.

`allow_migrate()` is reached from `BranchAwareRouter.allow_migrate()`, which has already
established that `db` is an alias your backend owns. Its default is written for a branch holding
only the branchable tables: migrations for the plugin's own models and for non-branchable models
are refused, because those tables are not in the branch schema at all. **A backend whose branch is
a full copy of main's database should override it to permit them** — there the tables do exist, and
refusing their migrations lets them drift out of step with main until a query selecting a column
main has and the branch lacks fails. Note that `model_name` is `None` for operations which name no
model (`RunPython`, `RunSQL`); keeping the default in that case is what stops a data migration from
being applied a second time to main, since under `activate_branch()` its ORM queries for
non-branchable models route there.

`Branch` retains all status transitions, [lifecycle signal](#lifecycle-signals) emission and `BranchEvent` creation around each of these calls, so those are unaffected by the backend in use. It also records whether a dataset currently exists: `provision()` returning without raising sets [`Branch.provisioned`](./models/branch.md#provisioned), and `deprovision()` clears it. A backend never writes that field itself, but everything which reaches for a branch connection reads it, so `provision()` must raise if it did not leave a usable dataset behind. A `provision()` which returns without having assigned an identifier is treated the same way: `Branch.provision()` raises `ImproperlyConfigured` on its behalf and marks the branch failed, rather than recording a dataset nothing can address.

### Branch Identity

A new `Branch` row has no `backend_id`. Assigning one is part of `provision()`'s contract, and must happen before the dataset is created, because the identifier is how every later connection addresses the branch:

```python
def provision(self, branch, user):
    if not branch.backend_id:
        branch.set_backend_id(self.generate_branch_id())
    ...
```

The value must be unique across all branches and no longer than 255 characters. `Branch.set_backend_id()` persists it, scoping the write to that one column so it cannot clobber the status transition `Branch.provision()` is wrapped in.

The identifier is **immutable once assigned** — it is what addresses the branch's dataset, so changing it would orphan the data the branch already holds. `set_backend_id()` raises `ValueError` on an attempt to change one, and assigning only when the field is empty means a re-provision — of an archived branch, or after a failed attempt — reuses the identifier the branch already had.

`SchemaBranchingBackend` generates a random eight-character alphanumeric string, kept short because the schema name it produces (`schema_prefix` + `backend_id`) must fit within PostgreSQL's 63-byte limit on identifiers. A backend addressing a remote service is free to use whatever identifier that service assigns.

### Connection Resolution

The two connection methods have deliberately different contracts, and the distinction matters:

- **`get_connection_alias(branch)`** is the single funnel every branch-aware query passes through, and a `Branch` row is always in hand. It **may** query the database. It is therefore the one place to read `branch.connection_params` (a nullable JSON field reserved for backend use — an out-of-tree backend cannot add its own migrations to this app) and hand the result to `register_connection_params()`.
- **`get_connection_config(alias, default_config)`** is called from inside Django's `ConnectionHandler` *while a connection is being created*. It **must not** query the database — doing so recurses. Retrieve anything you need via `get_registered_connection_params()` instead.

A backend that registers real endpoints or credentials must discard them in its own `deprovision()`, so that it does not hold a dead branch's credentials for the life of the process. `invalidate_connection(alias)` does that and drops the branch's open connection with it:

```python
def deprovision(self, branch):
    ...  # destroy the dataset
    self.invalidate_connection(self.get_connection_alias(branch))
```

Use the same call whenever an endpoint *moves* — a branch destroyed and rebuilt under the same name, say. Closing the connection is not enough on its own: Django calls `get_connection_config()` only when it creates a connection and keeps that `settings_dict` on the `DatabaseWrapper`, so a closed connection reopens against the old host and credentials. The wrapper has to go.

This is the backend's responsibility rather than the model's because the backend is the only party that can name the branch's alias without risking a failure, and the only one that knows when it has finished with the parameters. `Branch.delete()` runs the row delete and the deprovision in one transaction, so anything raised during teardown rolls the deletion back — which is what made branches whose dataset was already gone impossible to delete.

Both Django's connection handler and the registry are thread-local, so this clears the calling thread's state only; another thread which had addressed the same branch keeps its copy until that thread ends. A backend whose parameters can change must therefore not treat a registered entry as authoritative in a thread that has not itself re-derived it.

`owns_connection_alias()` may be broader than the set of aliases `get_connection_config()` can actually build a config for; returning `None` from the latter is how a backend declines one. `DynamicSchemaDict` resolves membership and lookup through the same call, so a declined alias is simply absent from `DATABASES` and Django raises its usual `ConnectionDoesNotExist` rather than a bare `KeyError` from inside connection creation.

### Configuration

A backend declares its own parameters, with their defaults, in `default_config`, and reads them with
`get_config()`:

```python
class MyBranchingBackend(BranchingBackend):
    connection_alias_prefix = 'mybackend_'

    default_config = {
        'cluster_endpoint': 'https://db.example.com',
        'clone_timeout': 300,
    }

    def provision(self, branch, user):
        endpoint = self.get_config('cluster_endpoint')
        ...
```

Operators set them under the plugin's [`backend_config`](./configuration.md#backend_config)
parameter, which keeps each backend's configuration namespaced from the plugin's own and from every
other backend's:

```python
PLUGINS_CONFIG = {
    'netbox_branching': {
        'backend': 'my_plugin.backends.MyBranchingBackend',
        'backend_config': {
            'cluster_endpoint': 'https://db.internal.example.com',
        },
    }
}
```

Resolution is per-parameter, not whole-dict: `clone_timeout` above still returns `300`, so an
operator setting one parameter does not have to restate the rest. `get_config()` raises `KeyError`
for a name absent from `default_config`, rather than returning `None` for it — a typo in the
backend's own source should fail where it is made, not as a mystery `None` somewhere downstream.

`get_config()` reads the configuration on each call, so it is safe from anywhere, including
`get_connection_config()`. Don't cache the result on the instance: `get_branching_backend()` caches
one instance per import path for the life of the process.

!!! note "`legacy_config_params`"
    `SchemaBranchingBackend` also lists `legacy_config_params = ('main_schema', 'schema_prefix', 'provision_workers')`, because those three predate `backend_config` and are still honoured at the root of the `netbox_branching` block. A root-level value loses to one under `backend_config`, and either way draws a `FutureWarning` at startup.

    This exists only for parameters with that history. A new backend leaves it empty: reading its parameters from the root would have it silently capture an unrelated plugin parameter of the same name.

### Presenting Backend-Specific Detail

The branch detail page shows only what the configured backend volunteers. Override `get_detail_fields(branch)` to return `(label, value)` pairs — a `None` value renders as a placeholder:

```python
def get_detail_fields(self, branch):
    if not branch.provisioned:
        return ((_('Cluster endpoint'), None),)
    return ((_('Cluster endpoint'), branch.connection_params['host']),)
```

Gate anything describing a live dataset on `branch.provisioned`: `backend_id` outlives the dataset, so a branch that has been archived still has an identifier to build a name from. `SchemaBranchingBackend` uses this to show its "Database schema" row, which is why that row does not appear under a backend that has no schema.

### Invariants

These are requirements of the surrounding machinery, not of any particular storage mechanism:

1. **Global primary key allocation.** Merge and revert replay each `ObjectChange.changed_object_id` verbatim against main, so the primary key of an object created within a branch must not collide with one allocated in main or in a sibling branch. `SchemaBranchingBackend` satisfies this by pointing each branch table's `id` default at main's sequence. A backend whose branches own independent sequences must partition the ID space between them.

2. **Empty changelog on a fresh branch.** Every `core.ObjectChange` row visible on the branch connection is treated as an unmerged branch change. A backend that copies main wholesale must truncate that table during provisioning; otherwise the first merge replays main's entire change history.

3. **Exempt-model visibility.** Non-branchable models (`auth.User`, `contenttypes`, `core.*`, the plugin's own models) are routed to main by `routes_model()`, but a join issued on the branch connection resolves against whatever *that* connection can see. A backend whose branch holds a point-in-time copy of those tables accepts display staleness there. Authentication and object permissions always evaluate against main, because those queries are routed there.

4. **Branch identity is assigned at provisioning time.** A `Branch` row exists in status "new" with `backend_id` unset; `provision()` is what gives the branch an identifier. Nothing may address the branch's dataset before then, and UI or API code which reads a branch's identifier must tolerate its absence. Note that the identifier outlives the dataset — it survives deprovisioning, and a failed `provision()` leaves behind whatever it had already assigned — so code asking whether a branch can be connected to must test `Branch.provisioned`, never `backend_id`. `Branch.ready`, which gates branch activation, tests it too: a branch in status "ready" whose dataset has gone missing cannot be activated.

### Registration

Set the [`backend`](./configuration.md#backend) configuration parameter to the import path of your class:

```python
PLUGINS_CONFIG = {
    'netbox_branching': {
        'backend': 'my_plugin.backends.MyBranchingBackend',
    }
}
```

The path is resolved once at startup, and `validate_configuration()` is called immediately, so a missing or invalid backend surfaces as an `ImproperlyConfigured` error at boot rather than on the first branch-aware query. The backend's own parameters go under [`backend_config`](./configuration.md#backend_config); see [Configuration](#configuration) above.

## Branches and Plugin Upgrades

If a plugin is installed or upgraded after branches have been created, the existing branch schemas will **not** automatically receive the new database migrations. Branches with outstanding migrations will be flagged with the **Pending Migrations** status and can be brought up to date using the **Migrate** action; until they are migrated, they cannot be activated or merged.

The recommended practice is to install or upgrade plugins before creating branches, and to merge or remove all open branches before upgrading a plugin that modifies existing models.
