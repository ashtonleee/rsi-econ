#!/usr/bin/env bash
set -euo pipefail

if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon is not running." >&2
    exit 1
fi

docker compose exec -T bridge python -c 'import httpx, json; r = httpx.get("http://127.0.0.1:8000/status", timeout=5.0); r.raise_for_status(); print(json.dumps(r.json(), indent=2, sort_keys=True))'
