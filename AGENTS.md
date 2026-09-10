# AGENTS.md — netbox-branching

## Repository Overview

`netbox-branching` is a NetBox plugin that adds git-like branching to the network source-of-truth platform. Each branch is an isolated PostgreSQL schema copy of the database; users make changes within a branch and merge back to the main schema. It is owned by NetBox Labs and runs inside NetBox as a Django app (`netbox_branching`, mounted at `/branching/`). Requires PostgreSQL (schema isolation), Redis (background jobs), and NetBox 4.7.0+. The supported NetBox version range is declared by `min_version` / `max_version` in `netbox_branching/__init__.py` and summarised per release in `COMPATIBILITY.md`; check those rather than relying on a version quoted here.

## Tech Stack

- Python (defer to `pyproject.toml`; currently `>=3.12`)
- NetBox (host app — minimum and maximum versions are pinned in `netbox_branching/__init__.py` `min_version` / `max_version`; `COMPATIBILITY.md` summarises the matrix)
- Django + Django REST Framework (NetBox's foundations)
- PostgreSQL (required — branch isolation depends on schema-level separation)
- Redis (required — background jobs use NetBox's job queue)
- Django's built-in test runner (`django.test.TestCase`-based, run via `manage.py test`)
- ruff for lint + format, plus djlint, codespell, yamllint and django-upgrade, all driven by
  pre-commit (`.pre-commit-config.yaml`; tool config lives in `pyproject.toml`)
- mkdocs + mkdocs-material for user-facing docs

Defer all version pins to `pyproject.toml` and `netbox_branching/__init__.py`.

## Repository Map

```text
.
├── netbox_branching/          — The Django app.
│   ├── __init__.py            — PluginConfig (name, version, min/max NetBox); validates settings in ready().
│   ├── choices.py             — ChoiceSet subclasses (branch status, merge strategy, event type).
│   ├── constants.py           — BRANCH_ACTIONS list.
│   ├── contextvars.py         — active_branch ContextVar (propagates through async automatically).
│   ├── database.py            — BranchAwareRouter — custom Django database router.
│   ├── error_report.py        — Error reporting helpers.
│   ├── events.py              — BranchEvent creation and management.
│   ├── filtersets.py          — BranchFilterSet, BranchEventFilterSet, ChangeDiffFilterSet.
│   ├── jobs.py                — AsyncJob subclasses: Provision, Sync, Merge, Revert, Migrate.
│   ├── middleware.py          — BranchMiddleware: per-request branch context activation.
│   ├── navigation.py          — Plugin menu definition.
│   ├── object_actions.py      — ObjectAction subclasses for branch operations.
│   ├── search.py              — SearchIndex registrations.
│   ├── signal_receivers.py    — Django signal handlers (post_save, pre_delete, etc.).
│   ├── signals.py             — Pre/post branch operation signals (pre_sync, post_merge, etc.).
│   ├── template_content.py    — PluginTemplateExtension registrations.
│   ├── urls.py                — Top-level URL routing.
│   ├── utilities.py           — DynamicSchemaDict, branch activation helpers, change replay.
│   ├── views.py               — All UI views.
│   ├── webhook_callbacks.py   — Webhook/event rule integration.
│   ├── api/
│   │   ├── serializers.py
│   │   ├── urls.py            — NetBoxRouter registrations.
│   │   └── views.py           — BranchViewSet (with sync/merge/revert/migrate actions).
│   ├── forms/
│   │   ├── bulk_edit.py
│   │   ├── bulk_import.py
│   │   ├── filtersets.py      — Filter forms for list views.
│   │   ├── misc.py
│   │   └── model_forms.py
│   ├── merge_strategies/
│   │   ├── strategy.py        — Abstract MergeStrategy base class.
│   │   ├── iterative.py       — IterativeMergeStrategy (default).
│   │   └── squash.py          — SquashMergeStrategy.
│   ├── migrations/            — Django schema migrations (0001–0008).
│   ├── models/
│   │   ├── __init__.py        — Star-imports every submodule.
│   │   ├── branches.py        — Branch, BranchEvent.
│   │   └── changes.py         — ObjectChange (proxy), ChangeDiff, AppliedChange.
│   ├── tables/
│   │   ├── columns.py
│   │   └── tables.py
│   ├── templatetags/
│   │   ├── branch_buttons.py
│   │   └── branch_filters.py
│   ├── templates/netbox_branching/
│   │   ├── buttons/
│   │   └── inc/
│   └── tests/
│       ├── utils.py                    — Shared test utilities.
│       ├── test_api.py
│       ├── test_branches.py
│       ├── test_changediff.py
│       ├── test_config.py
│       ├── test_connection_lifecycle.py
│       ├── test_events.py
│       ├── test_filtersets.py
│       ├── test_iterative_merge.py
│       ├── test_query.py
│       ├── test_related_models.py
│       ├── test_request.py
│       ├── test_squash_merge.py
│       ├── test_sync.py
│       └── test_views.py
├── docs/                      — mkdocs site.
│   ├── models/                — Per-model documentation.
│   ├── using-branches/        — User guides.
│   └── development/           — Maintainer guides (releasing).
├── testing/
│   └── configuration.py       — NetBox config used by the test workflow.
├── scripts/                   — Packaging verification scripts run by release.yaml.
│   ├── verify_release_tag.py  — Tag ↔ pyproject ↔ AppConfig ↔ wheel version consistency.
│   └── verify_wheel_contents.py — Wheel ships templates/migrations, not tests or bytecode.
├── .github/
│   ├── workflows/             — test.yml, release.yaml, claude-review.yml, no-blank-issue.yml.
│   ├── ISSUE_TEMPLATE/        — Issue forms.
│   ├── labels.yml             — Canonical NBL issue label set (applied with `gh label clone`).
│   └── pull_request_template.md
├── .copier-answers.yml        — Scaffold tracking; see "Scaffold" below. Never hand-edit.
├── .pre-commit-config.yaml    — Lint/format hook stack.
├── .yamllint                  — YAML lint rules for the yamllint hook.
├── AGENTS.md                  — This file.
├── CLAUDE.md                  — Shim that pulls in this file.
├── COMPATIBILITY.md           — Plugin → NetBox version matrix.
├── mkdocs.yml
└── pyproject.toml             — Plugin metadata, dependencies, and all tool config.
```

## Architecture

### Database Isolation

The core mechanism uses PostgreSQL schemas. Each branch gets its own schema (e.g. `branch_abc123`). Two custom components make this work:

- **`DynamicSchemaDict`** (`utilities.py`): A `dict` subclass wrapping `DATABASES`. When Django looks up a `schema_<id>` database alias, it returns the standard DB config with a modified `search_path` pointing to that branch's schema — without requiring pre-registration of every alias.
- **`BranchAwareRouter`** (`database.py`): A Django database router that intercepts all queries, checks the current `active_branch` context variable, and routes to the appropriate schema alias.

Both must be configured in the host NetBox instance (`DATABASES = DynamicSchemaDict(...)` and `DATABASE_ROUTERS` containing `BranchAwareRouter`). The plugin validates these in `AppConfig.ready()` and raises `ImproperlyConfigured` if either is missing.

### Context Management

- `contextvars.py`: Holds `active_branch` as a `ContextVar` — propagates through async code automatically.
- `middleware.py`: `BranchMiddleware` reads the active branch from cookies/query params and sets the context variable for each request, then restores it on teardown.
- `utilities.py`: `activate_branch()` / `deactivate_branch()` provide programmatic context switching used by jobs and tests.

### Branch Lifecycle

```
NEW → PROVISIONING → READY → (SYNCING / MIGRATING / MERGING / REVERTING) → MERGED or ARCHIVED
```

Transitional statuses (`PROVISIONING`, `SYNCING`, `MIGRATING`, `MERGING`, `REVERTING`) indicate a background job is in progress. `PENDING_MIGRATIONS` and `FAILED` are additional terminal-adjacent states.

Branch operations run as background jobs in `jobs.py`:

| Job class | Operation |
|---|---|
| `ProvisionBranchJob` | Create the schema and copy the database |
| `SyncBranchJob` | Pull changes from main into the branch |
| `MergeBranchJob` | Apply branch changes to main |
| `RevertBranchJob` | Undo a merged branch's changes |
| `MigrateBranchJob` | Apply outstanding Django migrations to the branch schema |

### Merge Strategies (`merge_strategies/`)

Pluggable strategy pattern with an abstract base in `strategy.py`. Selected per-branch via `Branch.merge_strategy`:

- **`IterativeMergeStrategy`** (default): Replays `ObjectChange` log entries in chronological order, one at a time.
- **`SquashMergeStrategy`**: Collapses all per-object changes into a single create/update/delete operation before applying. Handles dependency ordering via `CollapsedChange`.

Both strategies work by replaying NetBox's built-in `ObjectChange` audit trail. The abstract base provides `_clean()` post-merge cleanup; subclasses implement `merge()` and `revert()`.

### Change Tracking (`models/changes.py`)

- **`ObjectChange`** — Proxy of NetBox's built-in `ObjectChange` model. Adds `apply()`, `undo()`, and `migrate()` methods used by merge strategies.
- **`ChangeDiff`** — Tracks per-object diffs between a branch and main. Used for conflict detection: a conflict exists when the same object is modified in both main and the branch since the last sync.
- **`AppliedChange`** — Records which changes have been applied to branch schemas, enabling idempotent sync/migrate operations.

### Signals (`signals.py`)

The plugin exposes pre/post signals for every branch lifecycle operation, enabling integration by third-party code:

`pre_provision` / `post_provision`, `pre_deprovision` / `post_deprovision`, `pre_sync` / `post_sync`, `pre_migrate` / `post_migrate`, `pre_merge` / `post_merge`, `pre_revert` / `post_revert`

### Branch Action Validators

Callable validators can be registered for each action (`sync`, `merge`, `migrate`, `revert`, `archive`) via `PLUGINS_CONFIG`. These are loaded in `AppConfig.ready()` via `Branch.register_preaction_check()` and called before the corresponding job is enqueued. See `docs/plugin-development.md` for the validator signature.

### Key Files

| File | Role |
|---|---|
| `netbox_branching/__init__.py` | Plugin AppConfig, settings validation, signal registration |
| `netbox_branching/database.py` | `BranchAwareRouter` — schema routing |
| `netbox_branching/middleware.py` | Request-level branch activation |
| `netbox_branching/utilities.py` | `DynamicSchemaDict`, branch activation helpers, change replay |
| `netbox_branching/models/branches.py` | `Branch` and `BranchEvent` models |
| `netbox_branching/models/changes.py` | `ObjectChange` proxy, `ChangeDiff`, `AppliedChange` |
| `netbox_branching/merge_strategies/` | Pluggable merge implementations |
| `netbox_branching/jobs.py` | Background AsyncJob subclasses |
| `netbox_branching/signal_receivers.py` | Django ORM signal handlers |
| `testing/configuration.py` | Test NetBox configuration |

## Commands

There is no Justfile/Makefile in this repo; commands are raw. Run them inside a NetBox checkout that has this plugin installed and `testing/configuration.py` linked in as `netbox/netbox/configuration.py`.

| Command | What it does |
|---|---|
| `pip install -e '.[dev,test,docs]'` (from this repo) | Install the plugin in editable mode with all extras |
| `pre-commit install` | Install the git hooks (one time) |
| `pre-commit run --all-files` | Run the full lint/format stack |
| `python netbox/manage.py test netbox_branching.tests --keepdb` | Run the full test suite |
| `python netbox/manage.py test netbox_branching.tests.test_branches --keepdb` | Run a single test module |
| `ruff check` | Lint only (also runs via pre-commit) |
| `ruff format` | Format only (also runs via pre-commit) |
| `python netbox/manage.py makemigrations netbox_branching` | Generate Django migrations after model changes |
| `python netbox/manage.py migrate` | Apply migrations |
| `python netbox/manage.py runserver` | Start NetBox locally with the plugin loaded |
| `mkdocs serve` | Preview the user docs |
| `mkdocs build` | Build static docs site |
| `python -m build` | Build sdist + wheel (matches the release workflow) |

## Development

NetBox plugins must run inside a NetBox checkout. The reproducible setup mirrors what CI does (`.github/workflows/test.yml`):

1. Clone NetBox alongside this repo: `git clone https://github.com/netbox-community/netbox.git`
2. Symlink this repo's `testing/configuration.py` into NetBox: `ln -s "$PWD/nbl-netbox-branching/testing/configuration.py" netbox/netbox/netbox/configuration.py`
3. Install NetBox's requirements: `pip install -r netbox/requirements.txt`
4. Install this plugin in editable mode: `pip install -e '.[dev,test,docs]'`
5. Provision PostgreSQL (`netbox` / `netbox` / `netbox`) and Redis on localhost (default ports)
6. Run migrations and start the dev server

The `testing/configuration.py` sets `PLUGINS = ['netbox_branching']`, configures `DATABASES` as a `DynamicSchemaDict`, and adds `BranchAwareRouter` to `DATABASE_ROUTERS`.

After model changes, generate a migration with `python netbox/manage.py makemigrations netbox_branching`.

## Testing

- Tests use `django.test.TestCase`, **not** pytest. Suites live in `netbox_branching/tests/`.
- `tests/plugin_testing.py` provides plugin-namespace-aware bases (`PluginTestCases`,
  `PluginAPIViewTestCases`) that route `reverse()` through `plugins:` / `plugins-api:`.
  Use these instead of re-implementing `_get_base_url()` per test case.
- Run via NetBox's test runner: `python netbox/manage.py test netbox_branching.tests --keepdb`. The `--keepdb` flag preserves the test database and branch schemas between runs, which is important for speed.
- The runner uses NetBox's settings and creates a real PostgreSQL test database — branch schema provisioning and teardown happen against a real database. Do not mock the database.
- Test modules:

| Module | Coverage area |
|---|---|
| `test_api.py` | REST API endpoints (CRUD, sync/merge/revert/migrate actions) |
| `test_branches.py` | Branch model operations and lifecycle |
| `test_changediff.py` | `ChangeDiff` conflict detection |
| `test_config.py` | Plugin configuration validation |
| `test_connection_lifecycle.py` | Database connection management |
| `test_events.py` | `BranchEvent` creation |
| `test_filtersets.py` | Filterset functionality |
| `test_iterative_merge.py` | Iterative merge strategy (comprehensive) |
| `test_query.py` | Branch-aware query routing |
| `test_related_models.py` | Related model handling across schemas |
| `test_request.py` | Request-level branch context |
| `test_squash_merge.py` | Squash merge strategy (comprehensive) |
| `test_sync.py` | Branch sync operations (comprehensive) |
| `test_views.py` | Web views |

## CI/CD

GitHub Actions workflows in `.github/workflows/`:

- **`test.yml`** — Runs on every PR. Two jobs, the second gated on the first:
  - *Lint*: Python 3.12, runs the full `pre-commit` stack (which includes `mkdocs build`).
  - *Test*: Matrix of Python 3.12, 3.13, 3.14 against both the declared minimum NetBox version and `main`. Spins up PostgreSQL + Redis services, installs the plugin editable, loads `testing/configuration.py` via `NETBOX_CONFIGURATION` + `PYTHONPATH` (no symlink), and runs `python netbox/manage.py test netbox_branching.tests --keepdb`. The `main` leg can be pointed at another ref with a `test-against:<ref>` PR label or a `workflow_dispatch` input. One leg additionally collects coverage. The suite is not run with `--parallel`.
- **`release.yaml`** — Driven by pushing a `v*` tag, not by publishing a GitHub release, so pre-releases follow the same automated path as final releases. Builds sdist + wheel with `python -m build`, runs `twine check`, verifies the tag against the version declared in `pyproject.toml`, `AppConfig.version` and the wheel metadata (`scripts/verify_release_tag.py`), verifies the wheel's contents (`scripts/verify_wheel_contents.py`), rebuilds a wheel from the sdist, and smoke-tests a clean `--no-deps` install whose installed tree is held to the same content checks as the wheel. Only then does it publish to PyPI using OIDC trusted publishing and attach the artifacts to the GitHub release, drafting an empty one (marked as a pre-release when PEP 440 says the version is one) if the tag doesn't already have a release. Release notes are never generated — they are written by hand. Also runs — build and verification only, no publish — on pull requests that touch packaging inputs, which catches a version bump applied to only one of the two declaration sites. A `workflow_dispatch` from a `v*` tag publishes to Test PyPI instead, as an opt-in rehearsal.
- **`claude-review.yml`** — Claude Code automation hook; triggers on issue/PR comments mentioning `@claude`.

## Scaffold

This repo is tracked against [`netbox-plugin-scaffold`](https://github.com/netboxlabs/netbox-plugin-scaffold)
via `.copier-answers.yml`. Pull scaffold improvements with:

```
copier update --trust
```

`.copier-answers.yml` is generated — change the answers by re-running Copier, not by editing it.

The scaffold targets *private* plugins, so a few surfaces are deliberately divergent and any
`copier update` that touches them must be resolved in this repo's favour:

| Surface | Why it diverges |
|---|---|
| `.github/workflows/release.yaml` | The scaffold publishes to internal CodeArtifact. This plugin publishes to public PyPI via OIDC trusted publishing, with the tag/wheel verification in `scripts/`. Reject the scaffold's `release.yml` outright. |
| `docs/development/releasing.md` | Documents the PyPI flow above, not CodeArtifact. |
| `pyproject.toml` `[project].name` | Must stay `netboxlabs-netbox-branching`; the scaffold derives it from `repo_slug`. |
| `pyproject.toml` `[tool.setuptools]` | Kept as-is because `scripts/verify_wheel_contents.py` asserts against this exact packaging shape. |
| `testing/configuration.py` | Maintained by hand for this plugin's `DynamicSchemaDict` / `BranchAwareRouter` requirements. |
| `docs/changelog.md` | This repo's change log; the scaffold ships `docs/releases.md`. |
| `.yamllint` | The scaffold ships the `yamllint` hook but renders no config, so this one is adapted from the scaffold's own root config. |
| `.github/workflows/test.yml` | Keeps the `test-against:<ref>` label override and the `workflow_dispatch` ref input, which the scaffold has no equivalent for. Because those check out a caller-supplied NetBox ref and run it, the scaffold's `cache: pip` is removed from both jobs — CodeQL's `actions/cache-poisoning` rule flags package installs as poisonable once any cache exists in such a workflow. Do not reinstate the cache without also removing the dynamic ref. |

The scaffold's `ui/` and `graphql/` stub packages are intentionally absent — this plugin has
neither surface. A `copier update` will offer to add them; decline.

## Common Tasks

### Add a new model

1. Add the model to `models/branches.py` or `models/changes.py` (or a new module imported from `models/__init__.py`). Use NetBox's `PrimaryModel` for full features or `BaseModel` for auxiliary tables.
2. Run `python netbox/manage.py makemigrations netbox_branching`.
3. Wire up the rest of the surface area: `filtersets.py`, `forms/model_forms.py`, `forms/filtersets.py`, `tables/tables.py`, `api/serializers.py`, `api/urls.py`, `urls.py`, `navigation.py`, and a per-model template under `templates/netbox_branching/`.
4. Register a `SearchIndex` in `search.py` if the model should appear in NetBox's global search.
5. Add tests covering model logic, API, filtersets, and views.

### Add a REST API endpoint

1. Add the serializer to `api/serializers.py` — `NetBoxModelSerializer` for `PrimaryModel`.
2. Add the viewset to `api/views.py`. For custom actions (sync, merge, etc.) use `@action(detail=True, methods=['post'])`.
3. Register the route in `api/urls.py` via the `NetBoxRouter`.
4. Ensure a corresponding `FilterSet` exists in `filtersets.py`; add explicit `<field>_id = ModelMultipleChoiceFilter(field_name='<field>', ...)` for FK filters.
5. Add an integration test in `tests/test_api.py`.

### Add a branch action validator

1. Write a callable with signature `def my_validator(branch, user, **kwargs)` that raises `ValidationError` to block the action.
2. Users configure it in `PLUGINS_CONFIG['netbox_branching']['<action>_validators'] = ['myapp.validators.my_validator']`.
3. The plugin loads and registers validators in `AppConfig.ready()` — no code changes required in the plugin itself.
4. Document the validator contract in `docs/plugin-development.md`.

### Bump the supported NetBox version

1. Update `min_version` / `max_version` in `netbox_branching/__init__.py`.
2. Update `COMPATIBILITY.md`.
3. Adjust the NetBox refs in the `.github/workflows/test.yml` matrix, and the
   `netbox_min_version` / `netbox_max_version` / `netbox_test_min_ref` / `netbox_test_max_ref`
   answers in `.copier-answers.yml`.
4. Run the suite locally against the new version.
5. Note any compatibility changes or breaking changes in `docs/changelog.md`.

### Cut a release

See [`docs/development/releasing.md`](./docs/development/releasing.md) for the user-facing version.

1. Bump `version` in both `pyproject.toml` and `netbox_branching/__init__.py`. The two must agree — `release.yaml` fails the build if they don't, on release PRs as well as on the tag itself.
2. Update `docs/changelog.md`.
3. Push a `vX.Y.Z` tag. `release.yaml` builds, verifies, publishes to PyPI, and attaches the sdist and wheel to the GitHub release.
4. Write the release notes. If the release was created up front, the tag push just attaches the artifacts to it; if the tag was pushed on its own, the workflow leaves an empty draft release to paste the notes into and publish. Nothing is ever generated from commit messages.

Tags must match `vX.Y.Z[designation]`. The designation is what makes a pre-release, and versions are compared after PEP 440 normalisation, so a beta is cut by setting the version to `1.3.0b1` in both files and pushing either spelling of the tag:

```
git tag v1.3.0-beta1 && git push origin v1.3.0-beta1   # or: git tag v1.3.0b1
```

PyPI receives `1.3.0b1`, which `pip install netboxlabs-netbox-branching` skips unless the user opts in with `--pre` or pins the exact version, and the draft GitHub release is marked as a pre-release automatically.

To rehearse a publish without touching production PyPI, run the workflow manually (`workflow_dispatch`) against the tag; that route publishes to Test PyPI and requires a trusted publisher configured there for this project.

## Conventions and Patterns

- **Plugin code stays in the plugin package.** Don't monkey-patch NetBox.
- **Use NetBox's mixins and base classes** (`PrimaryModel`, `BaseModel`, `NetBoxModelSerializer`, `NetBoxModelFilterSet`) rather than re-implementing behaviour.
- **All UI views use `@register_model_view`** from `utilities.views`. All views live in `views.py`.
- **FK filters** must declare an explicit `<field>_id = ModelMultipleChoiceFilter(field_name='<field>', ...)` in the filterset; do not rely on `Meta.fields` to auto-generate `_id` variants.
- **Signal receivers** for Django ORM events (`post_save`, `pre_delete`, etc.) live in `signal_receivers.py` and are imported in `AppConfig.ready()`.
- **Branch operation signals** (pre/post lifecycle events) are defined in `signals.py` and documented for third-party use.
- **Cross-model UI extensions** live in `template_content.py`.
- **Search registration** lives in `search.py`.
- **Permissions** use NetBox's standard model permissions (`netbox_branching.view_branch`, etc.).
- **Exempt models.** Plugin models that should not be branched must be listed in `PLUGINS_CONFIG['netbox_branching']['exempt_models']`. Other plugin authors are responsible for configuring this for their own models.
- **Migrations.** No squashing has been done; migrations are sequential (0001–0008). Write data migrations using `apps.get_model(...)` and `get_or_create` — ContentType rows may not exist at migration time.
- **Connection cleanup.** Branch database aliases are dynamically created and not in `DATABASES.keys()`, so Django's built-in `close_old_connections()` misses them. `close_old_branch_connections()` in `utilities.py` is connected to `request_started`/`request_finished` signals to plug this leak (see issue #358).
- **Linting.** All tool config lives in `pyproject.toml`; there is deliberately no `ruff.toml`. Ruff runs with `preview = true`, line length 120, single quotes, LF endings, and the scaffold's rule set (pycodestyle, pyflakes, isort, pyupgrade, comprehensions, return, simplify, pathlib, bugbear and logging subsets). Ignored: `F403`, `F405`, `RET504`, `SIM102`, `SIM114`, `UP032`. NetBox core apps and `netbox_branching` are both treated as first-party for import sorting. Run everything with `pre-commit run --all-files` rather than invoking the tools individually.

## Troubleshooting

- **`DATABASES must be a DynamicSchemaDict instance`** — The host NetBox configuration has not wrapped `DATABASES` with `DynamicSchemaDict`. See the README installation instructions.
- **`DATABASE_ROUTERS must contain 'netbox_branching.database.BranchAwareRouter'`** — Add the router string to `DATABASE_ROUTERS` in the NetBox configuration.
- **Branch stuck in a transitional status** — A background job likely failed. Check the job log in the NetBox UI or database. The job timeout is configurable via `job_timeout` (default 3600 s).
- **Conflict detected on merge** — `ChangeDiff` found that the same object was modified in both main and the branch since the last sync. Sync the branch first to incorporate main's changes, then re-attempt the merge.
- **`PENDING_MIGRATIONS` status** — The branch schema is missing Django migrations that have been applied to main. Run `MigrateBranchJob` (or use the Migrate button in the UI) to apply them.
- **Tests fail with connection errors** — Ensure PostgreSQL and Redis are running and accessible. The test config in `testing/configuration.py` expects both on localhost default ports.
- **Branch connections leak over time** — Likely missing the `request_started`/`request_finished` signal hookup for `close_old_branch_connections`. This is registered automatically in `AppConfig.ready()`; confirm the plugin loaded correctly.

## References

- Plugin README: [`README.md`](./README.md)
- Compatibility matrix: [`COMPATIBILITY.md`](./COMPATIBILITY.md)
- User docs (mkdocs): [`docs/`](./docs/)
- Plugin development guide: [`docs/plugin-development.md`](./docs/plugin-development.md)
- NetBox plugin docs: <https://netboxlabs.com/docs/netbox/plugins/>
- Project scaffold: <https://github.com/netboxlabs/netbox-plugin-scaffold>
