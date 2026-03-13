#!/usr/bin/env bash
set -euo pipefail

if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon is required for Stage 5 tests." >&2
    exit 1
fi

python -m pytest
