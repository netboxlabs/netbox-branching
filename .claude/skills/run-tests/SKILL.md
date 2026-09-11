---
name: run-tests
description: Run the netbox_branching plugin's Django test suite against a local NetBox checkout. Use when the user asks to run tests, run a specific test module/class/method, or verify changes pass before opening a PR.
---

# Run the plugin's test suite

This plugin uses Django's built-in test runner (`django.test.TestCase`), **not** pytest — `pyproject.toml` lists `pytest` as a test extra but the suite is invoked via NetBox's `manage.py test`. CI runs this exact command in `.github/workflows/test.yml`.

## Canonical command

From the NetBox repo root (with the plugin installed in editable mode and `testing/configuration.py` linked into NetBox):

```bash
python netbox/manage.py test netbox_branching.tests --keepdb
```

This is the same command CI runs. Add `-v 2` to print each test as it executes; drop to `-v 1` for terser output.

## Prerequisites (one-time setup)

1. NetBox checkout alongside this repo (`../netbox` or any sibling path).
2. `testing/configuration.py` made visible to NetBox. CI does this without touching the
   NetBox checkout, and so can you:
   ```bash
   export NETBOX_CONFIGURATION=configuration
   export PYTHONPATH="$PWD/testing"
   ```
   A symlink into `../netbox/netbox/netbox/configuration.py` also works, but overwrites
   whatever config that checkout already has. This config sets `PLUGINS = ['netbox_branching']`, wraps `DATABASES` with `DynamicSchemaDict`, adds `BranchAwareRouter` to `DATABASE_ROUTERS`, and points at a local Postgres + Redis on default ports (`netbox` / `netbox` / `netbox`).
3. Plugin installed in editable mode with test extras:
   ```bash
   pip install -e '.[dev,test,docs]'
   ```
4. NetBox dependencies installed: `pip install -r ../netbox/requirements.txt`.
5. Postgres + Redis reachable on localhost (defaults).

If any of these are missing, surface the gap to the user — do not silently skip.

## Useful variants

Run a single test module / class / method (Django's dotted-path target):

```bash
python netbox/manage.py test netbox_branching.tests.test_branches --keepdb
python netbox/manage.py test netbox_branching.tests.test_api --keepdb
python netbox/manage.py test netbox_branching.tests.test_iterative_merge.IterativeMergeTestCase --keepdb
```

Available test modules in `netbox_branching/tests/`:
- `test_api` — REST API endpoints (CRUD, sync/merge/revert/migrate actions).
- `test_branches` — Branch model operations and lifecycle.
- `test_changediff` — `ChangeDiff` conflict detection.
- `test_config` — Plugin configuration validation.
- `test_connection_lifecycle` — Database connection management.
- `test_events` — `BranchEvent` creation.
- `test_filtersets` — Filterset behaviour (FK `_id` filters, etc.).
- `test_iterative_merge` — Iterative merge strategy (comprehensive).
- `test_query` — Branch-aware query routing.
- `test_related_models` — Related model handling across schemas.
- `test_request` — Request-level branch context.
- `test_squash_merge` — Squash merge strategy (comprehensive).
- `test_sync` — Branch sync operations (comprehensive).
- `test_views` — UI views via NetBox's test client.

Stop on first failure: `--failfast`. Run in parallel: `--parallel auto` (note: `--keepdb` and `--parallel` don't always compose cleanly).

## After model changes

Generate migrations before running tests, otherwise the test DB build will fail:

```bash
python netbox/manage.py makemigrations netbox_branching
```

## Why these choices

- **Don't substitute pytest.** The suite uses `django.test.TestCase`; pytest would need `pytest-django` configured against NetBox's settings, which nobody has set up. Run via `manage.py test` to match CI.
- **Always use `--keepdb`.** Branch schema provisioning and teardown hit a real PostgreSQL database. Recreating the test DB on every run is slow and unnecessary; `--keepdb` preserves it between runs.
- **Don't mock the database.** Tests exercise the real ORM, views, and APIs end-to-end. The whole point of this plugin is schema-level database isolation — a mocked DB can't exercise that.
- **Match CI's invocation.** If a test passes locally but fails in CI, the first diagnostic is "did you run the same command?" — keeping the canonical form identical removes that variable.

## References

- [`AGENTS.md`](../../../AGENTS.md) "Testing" and "Development" sections — environment setup and layout.
- [`.github/workflows/test.yml`](../../../.github/workflows/test.yml) — authoritative CI invocation.
- [`testing/configuration.py`](../../../testing/configuration.py) — NetBox config the test runner uses.
