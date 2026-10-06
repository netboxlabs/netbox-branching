# Releasing

Releases are driven entirely by pushing a `vX.Y.Z` tag; `.github/workflows/release.yaml`
does the rest. Release notes are always written by hand — nothing is generated from
commit messages.

## Checklist

1. **Bump the version in both declaration sites.** `version` in `pyproject.toml` and
   `AppConfig.version` in `netbox_branching/__init__.py` must agree; the release
   workflow fails the build if they don't, on release PRs as well as on the tag.

2. **Update the change log.** Add the release's entries to `docs/changelog.md`.

3. **Push the tag.**

   ```bash
   git tag v1.2.3 && git push origin v1.2.3
   ```

   The workflow builds an sdist and wheel, runs `twine check`, verifies the tag against
   `pyproject.toml`, `AppConfig.version` and the wheel metadata
   (`scripts/verify_release_tag.py`), verifies the wheel ships templates and migrations
   but not tests or bytecode (`scripts/verify_wheel_contents.py`), rebuilds a wheel from
   the sdist, and smoke-tests a clean `--no-deps` install. Only then does it publish to
   PyPI via OIDC trusted publishing and attach the artifacts to the GitHub release.

4. **Write the release notes.** If the GitHub release already existed, the tag push just
   attaches artifacts to it. If the tag was pushed on its own, the workflow leaves an
   empty draft release to paste the notes into and publish.

## Pre-releases

The tag must match `vX.Y.Z[designation]`, and the designation is what makes a
pre-release. Versions are compared after PEP 440 normalisation, so a beta is cut by
setting the version to `1.3.0b1` in both files and pushing either spelling:

```bash
git tag v1.3.0-beta1 && git push origin v1.3.0-beta1   # or: git tag v1.3.0b1
```

PyPI receives `1.3.0b1`, which `pip install netboxlabs-netbox-branching` skips unless the
user opts in with `--pre` or pins the exact version. The draft GitHub release is marked
as a pre-release automatically.

## Rehearsing a publish

Running the release workflow manually (`workflow_dispatch`) from a `v*` tag publishes to
Test PyPI instead of production PyPI. This requires a trusted publisher configured for
the project on Test PyPI.
