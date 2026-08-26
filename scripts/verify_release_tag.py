#!/usr/bin/env python3
"""Verify the release tag and the version declarations all agree with the built wheel.

Usage: verify_release_tag.py [--tag <git-ref-or-tag>] <wheel>

The plugin declares its version in two places which must stay in lockstep:

  * ``version`` in pyproject.toml — what ends up in the distribution metadata
  * ``AppConfig.version`` in netbox_branching/__init__.py — what NetBox displays

With ``--tag``, the git tag being released is checked against both as well. Versions
are compared after PEP 440 normalisation, so the tag ``v1.2.0-beta1``, the pyproject
version ``1.2.0b1`` and the AppConfig version ``1.2.0-beta1`` are all considered equal.

Without ``--tag`` (pull requests, branch dispatches) the tag comparison is skipped and
only the two in-repo declarations are checked against the wheel.
"""

import argparse
import re
import sys
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / 'pyproject.toml'
APPCONFIG = REPO_ROOT / 'netbox_branching' / '__init__.py'

APPCONFIG_VERSION_RE = re.compile(r"^\s*version\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)


def wheel_version(wheel_path):
    """Return the Version metadata declared by a built wheel."""
    with zipfile.ZipFile(wheel_path) as archive:
        meta_name = next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))
        metadata = Parser().parsestr(archive.read(meta_name).decode())
    return metadata['Version']


def pyproject_version():
    """Return the version declared in pyproject.toml."""
    with PYPROJECT.open('rb') as f:
        return tomllib.load(f)['project']['version']


def appconfig_version():
    """Return AppConfig.version from the plugin package, without importing Django."""
    match = APPCONFIG_VERSION_RE.search(APPCONFIG.read_text())
    if match is None:
        raise SystemExit(f'Could not find a version declaration in {APPCONFIG}')
    return match.group(1)


def normalize(label, value):
    """PEP 440-normalise a version string, stripping a leading 'v' and any ref prefix."""
    version = value.rsplit('/', 1)[-1].removeprefix('v')
    try:
        return str(Version(version))
    except InvalidVersion:
        raise SystemExit(f'{label} is not a valid PEP 440 version: {value}') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', help='the git ref or tag being released (e.g. v1.2.0 or v1.2.0-beta1)')
    parser.add_argument('wheel', help='path to the built wheel')
    args = parser.parse_args(argv)

    versions = {
        'wheel metadata': normalize('wheel metadata', wheel_version(args.wheel)),
        'pyproject.toml': normalize('pyproject.toml', pyproject_version()),
        'AppConfig.version': normalize('AppConfig.version', appconfig_version()),
    }
    if args.tag:
        versions['git tag'] = normalize('git tag', args.tag)

    for source, version in versions.items():
        print(f'{source}: {version}')

    if len(set(versions.values())) > 1:
        print('\nVersion mismatch: every source above must declare the same version.')
        return 1

    version = next(iter(versions.values()))
    if Version(version).is_prerelease:
        print(f'\nOK: {version} (pre-release)')
    else:
        print(f'\nOK: {version}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
