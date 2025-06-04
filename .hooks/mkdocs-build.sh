#!/bin/bash
set -euo pipefail

echo "Running mkdocs build..."

TEMP_SITE_DIR=$(mktemp -d)

# Run mkdocs build into temp dir
if mkdocs build --site-dir "$TEMP_SITE_DIR" --strict; then
    echo "Build successful."
else
    echo "Build failed." >&2
    exit 1
fi

# Cleanup
rm -rf "$TEMP_SITE_DIR"
