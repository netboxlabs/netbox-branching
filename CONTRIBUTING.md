# Contributing to NetBox Branching

First off, thanks for taking the time to contribute to **NetBox Branching**.
Contributions of all kinds are welcome. Please be kind, constructive, and
respectful in issues, PRs, and discussions.

---

## General tips for working on GitHub

- Register for a free [GitHub account](https://github.com/signup) if you
  haven't already.
- You can use [GitHub Markdown](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax)
  for formatting text and adding images.
- To help mitigate notification spam, please avoid "bumping" issues with
  no activity. To vote an issue up or down, use thumbs-up / thumbs-down
  reactions.
- Please avoid pinging members with `@` unless they have previously
  expressed interest or involvement with that particular issue.
- Familiarize yourself with this list of
  [discussion anti-patterns](https://github.com/bradfitz/issue-tracker-behaviors)
  and make every effort to avoid them.

---

## How we work: issue-first, then assignment, then PR

To avoid wasted effort and keep the project coherent, we follow an
**issue-first** workflow:

1. **Open an issue** (bug report or feature request) first.
2. A maintainer **triages** it. If it is viable, we mark it as
   **status: accepted** and (optionally) **status: needs owner**.
3. The issue's author or another volunteer can **offer to own it** by
   commenting.
4. A maintainer **assigns** the issue to the owner.
5. The owner opens a **pull request** that resolves the issue.

**Please do not open PRs without an accepted, assigned issue.** Unassigned
PRs may be closed to keep the queue focused. Draft PRs are welcome **after**
an issue is accepted and assigned.

> For background, see the NetBox contributing guide:
> <https://github.com/netbox-community/netbox/blob/main/CONTRIBUTING.md>

---

## Types of contributions

- **Report bugs.** See [Reporting bugs](#reporting-bugs).
- **Implement features.** See [Requesting features](#requesting-features).
  For issues tagged `help wanted`, comment to volunteer; a maintainer must
  assign the issue before work begins.
- **Fix bugs.** Look for issues tagged `type: bug` and `help wanted` (same
  assignment note as above).
- **Write documentation.** Improve the docs site, README, in-code
  docstrings, or tutorials.
- **Submit feedback.** See [Requesting features](#requesting-features) for
  the suggested structure.

---

## Reporting bugs

Open an issue and pick the **Bug report** template. Please include:

- **NetBox version** and **plugin version**.
- **Steps to reproduce** (clear and minimal).
- **Expected vs. actual behavior**.
- Any **stack traces, logs, or screenshots**.

Bug reports are for unintended behavior only; new functionality belongs in
a **feature request**.

---

## Requesting features

When proposing features, please provide:

- **Problem / use case** (why this matters for plugin users).
- **Proposed behavior** (what changes, at a high level).
- Any anticipated **models / UI / API** impacts.
- **Alternatives** you considered.

We may ask questions to refine the scope before acceptance.

---

## Development environment

This plugin targets the NetBox ecosystem (Django). You will need a working
NetBox development environment.

**Prerequisites:**

- Python 3.12 or newer.
- PostgreSQL and Redis (NetBox runtime requirements).
- A local NetBox checkout. Clone
  <https://github.com/netbox-community/netbox> next to this plugin so the
  layout looks like:

  ```text
  workspace/
    netbox/                        # NetBox core checkout
    netbox-branching/   # this plugin
  ```

- [`uv`](https://docs.astral.sh/uv/) for managing tool installs (optional
  but recommended; the scaffold uses `uv` for `copier`, `pre-commit`, and
  `ruff`).

**Setup:**

1. **Clone this repository** and create or activate a virtual environment.

2. **Install the plugin in editable mode** from the repo root, including
   the `dev`, `test` and `docs` extras for packaging, testing and building
   documentation:

   ```bash
   pip install -e '.[dev,test,docs]'
   ```

3. **Install pre-commit hooks** so lint runs locally on every commit:

   ```bash
   pip install pre-commit
   pre-commit install
   pre-commit run --all-files
   ```

4. **Run the test suite.** This plugin ships a `testing/configuration.py`
   for NetBox to load when running tests. Loading happens via the
   `NETBOX_CONFIGURATION` environment variable plus a prepended
   `PYTHONPATH`, never via a symlink into NetBox:

   ```bash
   export NETBOX_CONFIGURATION=configuration
   export PYTHONPATH=$PWD/testing:$PYTHONPATH
   python ../netbox/netbox/manage.py test netbox_branching.tests --keepdb
   ```

5. **Run NetBox** and verify the plugin loads and behaves as expected.

See `AGENTS.md` for the full development recipe, including the
`DynamicSchemaDict` / `BranchAwareRouter` database settings the plugin
requires and migration handling.

### Database migrations (Django)

- Include **one logical migration per PR** for model changes.
- Prefer **backward-compatible** changes; avoid destructive operations in
  the same release (deprecate first when possible).
- Use `RunPython` or `RunSQL` for **data migrations**; keep them
  idempotent and fast.
- Avoid surprising nullability or index changes on large tables without
  discussion.

### API and UI compatibility

- Avoid breaking API fields, choices, or slugs without prior deprecation.
- Keep UI patterns consistent with NetBox (tables, filtersets, views).

---

## Style, linting, and versions

- **Python style:** PEP 8 where practical; readability over rigid line
  limits. Ruff handles enforcement.
- **Linters and formatters:** We use **Ruff** via **pre-commit**. Run
  `pre-commit run --all-files` locally before pushing.
- **Supported Python:** Keep changes compatible with the versions tested
  in CI (currently 3.12, 3.13, 3.14).
- **Supported NetBox:** 4.7.0 to 4.7.99
  (matches the `PluginConfig` declaration in
  `netbox_branching/__init__.py`).
- **Django:** 6.1 (matches the supported NetBox runtime).
- **Typing:** Prefer adding or improving type hints where it increases
  clarity.

---

## Pull request guidelines

A PR is reviewed only if:

- It **links to an accepted, assigned issue** with `Fixes: #NNN` in the
  PR description.
- It **adds tests** where applicable.
- It **updates docs** for user-facing changes.
- **pre-commit** passes locally.
- It **does not** bump versions or edit the changelog. Maintainers
  handle release bookkeeping (see
  [`docs/development/releasing.md`](https://github.com/netboxlabs/netbox-branching/blob/main/docs/development/releasing.md)).
- **PR title follows the repo convention:** `Fixes #NNN: <summary>` for bugs,
  `Closes #NNN: <summary>` for features.

### Branching and commits

- Use clear, focused branches like `fix-<short-description>` or
  `feat-<short-description>`.
- PRs are squash-merged, so the PR title becomes the commit message on the
  default branch (e.g. `Fixes #732: Skip tables missing from main when
  provisioning a branch`).
- Keep PRs small and focused. Large refactors should be discussed first
  in an issue.

### Target branch

- Open PRs against the default branch unless a maintainer specifies
  otherwise.

---

## Documentation

- Update README or in-repo docs when behavior changes.
- Include short examples or screenshots for UI-adjacent changes.
- Keep docstrings current for public methods, models, and utilities.
- Build the docs locally with `zensical build --strict` before submitting
  documentation changes.

---

## Security

Please do **not** open a public issue for security problems. Follow our
[`SECURITY.md`](https://github.com/netboxlabs/netbox-branching/blob/main/SECURITY.md). If in doubt, contact a maintainer privately
and we will coordinate a fix and disclosure.

---

## Releasing (maintainers)

See [`docs/development/releasing.md`](https://github.com/netboxlabs/netbox-branching/blob/main/docs/development/releasing.md) for
the full release checklist.

---

## Thanks!

Whether you are filing a precise bug report, improving docs, or
implementing new functionality, thank you. Your time and effort are
appreciated.
