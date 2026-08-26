#!/usr/bin/env python3
"""Verify a built wheel ships the runtime data NetBox needs and nothing it shouldn't.

Usage: verify_wheel_contents.py <wheel>

The wheel's contents come entirely from the ``package-data`` glob in pyproject.toml, so
a packaging change can silently drop the templates (leaving a plugin that installs fine
and then 500s on every view) or sweep in artefacts from a dirty working tree. Both
failure modes are caught here, before anything is published.
"""

import sys
import zipfile
from pathlib import PurePosixPath

PACKAGE = 'netbox_branching'

# Runtime-critical files. Templates are loaded by path at render time and migrations by
# name, so neither is exercised by an import check — they have to be verified as data.
REQUIRED_FILES = {
    f'{PACKAGE}/__init__.py',
    f'{PACKAGE}/database.py',
    f'{PACKAGE}/middleware.py',
    f'{PACKAGE}/utilities.py',
    f'{PACKAGE}/migrations/0001_initial.py',
    f'{PACKAGE}/templates/{PACKAGE}/branch.html',
}
REQUIRED_PREFIXES = (
    f'{PACKAGE}/api/',
    f'{PACKAGE}/forms/',
    f'{PACKAGE}/merge_strategies/',
    f'{PACKAGE}/migrations/',
    f'{PACKAGE}/models/',
    f'{PACKAGE}/tables/',
    f'{PACKAGE}/templates/{PACKAGE}/',
    f'{PACKAGE}/templates/{PACKAGE}/buttons/',
    f'{PACKAGE}/templates/{PACKAGE}/inc/',
    f'{PACKAGE}/templatetags/',
)

# Never ship these. The test suite is excluded via exclude-package-data and needs a full
# NetBox checkout to run; compiled bytecode and editable-install leftovers only appear
# when a build is run over a dirty tree.
FORBIDDEN_PREFIXES = (f'{PACKAGE}/tests/',)
FORBIDDEN_SEGMENTS = (
    '__pycache__',
    '.pytest_cache',
    '.ruff_cache',
)
FORBIDDEN_SEGMENT_SUFFIXES = ('.egg-info',)


def members(wheel_path):
    with zipfile.ZipFile(wheel_path) as archive:
        return [name for name in archive.namelist() if not name.endswith('/')]


def main(argv):
    if len(argv) != 2:
        print('usage: verify_wheel_contents.py <wheel>')
        return 2
    names = members(argv[1])
    errors = []

    errors += [f'missing required file: {name}' for name in sorted(REQUIRED_FILES) if name not in names]
    errors += [
        f'no files found under required prefix: {prefix}'
        for prefix in REQUIRED_PREFIXES
        if not any(name.startswith(prefix) for name in names)
    ]

    for name in sorted(names):
        if name.startswith(FORBIDDEN_PREFIXES):
            errors.append(f'unexpected file: {name}')
        elif any(
            segment in FORBIDDEN_SEGMENTS or segment.endswith(FORBIDDEN_SEGMENT_SUFFIXES)
            for segment in PurePosixPath(name).parts
        ):
            errors.append(f'build artefact leaked into the wheel: {name}')
        elif name.endswith(('.pyc', '.pyo')):
            errors.append(f'compiled bytecode leaked into the wheel: {name}')

    if errors:
        print(f'{argv[1]} failed verification:')
        for error in errors:
            print(f'  - {error}')
        return 1

    print(f'OK: {argv[1]} contains {len(names)} members and passes all checks')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
