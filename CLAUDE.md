# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NetBox Branching is a Django plugin for [NetBox](https://github.com/netbox-community/netbox) that adds git-like branching functionality to the network source-of-truth platform. Users can create isolated PostgreSQL schema copies of the database, make changes within them, and merge back to the main schema. Requires PostgreSQL (for schema isolation), Redis (for background jobs), and NetBox 4.4+.

## Commands

### Linting
```bash
ruff check
```

### Testing
Tests require a running NetBox instance configured with this plugin. The test configuration is in `testing/configuration.py`.

```bash
# Full test suite
python netbox/manage.py test netbox_branching.tests --keepdb

# Single test module
python netbox/manage.py test netbox_branching.tests.test_branches --keepdb
```

CI runs: `python manage.py test netbox_branching.tests --keepdb` with PostgreSQL and Redis services.

### Documentation
```bash
mkdocs serve   # local dev server
mkdocs build   # build static site
```

## Architecture

### Database Isolation
The core mechanism uses PostgreSQL schemas. Each branch gets its own schema (e.g., `branch_abc123`). Two custom components make this work:

- **`DynamicSchemaDict`** (`utilities.py`): A dict subclass wrapping `DATABASES`. When Django looks up a `schema_<id>` database alias, it returns the standard DB config with a modified `search_path` pointing to that branch's schema.
- **`BranchAwareRouter`** (`database.py`): A Django database router that intercepts all queries, checks the current `active_branch` context variable, and routes to the appropriate schema alias.

### Context Management
- `contextvars.py`: Holds `active_branch` as a `ContextVar` — propagates through async code automatically.
- `middleware.py`: `BranchMiddleware` reads the active branch from cookies/query params and sets the context variable for each request.

### Branch Lifecycle
`NEW` → `PROVISIONING` → `READY` → (`MERGING`/`SYNCING`) → `MERGED` or `ARCHIVED`

Branch operations (sync, merge, revert, migrate) run as background jobs defined in `jobs.py`.

### Merge Strategies (`merge_strategies/`)
Pluggable strategy pattern with an abstract base in `strategy.py`:
- **`IterativeMergeStrategy`** (default): Replays ObjectChange log entries in chronological order.
- **`SquashMergeStrategy`**: Collapses all branch changes into single create/update/delete operations.

Both work by replaying NetBox's built-in `ObjectChange` audit trail entries.

### Change Tracking
- `models/changes.py`: `ChangeDiff` tracks per-object diffs between a branch and main, used for conflict detection.
- Conflicts occur when the same object is modified in both main and the branch since the last sync.

### Key Files
| File | Role |
|------|------|
| `netbox_branching/__init__.py` | Plugin AppConfig, signal registration, dependency validation |
| `netbox_branching/database.py` | `BranchAwareRouter` — schema routing |
| `netbox_branching/middleware.py` | Request-level branch activation |
| `netbox_branching/utilities.py` | `DynamicSchemaDict`, branch activation helpers, change replay |
| `netbox_branching/models/branches.py` | `Branch` model with core sync/merge/revert logic |
| `netbox_branching/merge_strategies/` | Pluggable merge implementations |
| `netbox_branching/jobs.py` | Background AsyncJob subclasses |
| `testing/configuration.py` | Test NetBox configuration |

## Code Style
- Line length: 120 characters
- Quotes: single
- Ruff rules: E1-E3, E501, W, I, RET, UP
