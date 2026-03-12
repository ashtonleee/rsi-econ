#!/usr/bin/env bash
set -euo pipefail

if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon is not running." >&2
    exit 1
fi

docker compose up --build -d --wait
