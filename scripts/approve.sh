#!/usr/bin/env bash
set -euo pipefail

if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon is not running." >&2
    exit 1
fi

if [ -z "${RSI_OPERATOR_TOKEN:-}" ]; then
    echo "Error: RSI_OPERATOR_TOKEN is not set. Export it before running this script." >&2
    exit 1
fi

TOKEN="$RSI_OPERATOR_TOKEN"
BASE="http://127.0.0.1:8000"

usage() {
    cat <<EOF
Usage: $0 <command> [options]

Commands:
  list [--status pending|approved|rejected|executed]
  show <proposal_id>
  approve <proposal_id> [--reason "..."]
  reject <proposal_id> [--reason "..."]
  execute <proposal_id>
EOF
    exit 1
}

run_in_bridge() {
    docker compose exec -T bridge python -c "$1"
}

COMMAND="${1:-}"
[ -z "$COMMAND" ] && usage
shift

case "$COMMAND" in
    list)
        STATUS_FILTER=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --status) STATUS_FILTER="$2"; shift 2 ;;
                *) echo "Unknown option: $1" >&2; exit 1 ;;
            esac
        done
        QUERY=""
        [ -n "$STATUS_FILTER" ] && QUERY="?status=${STATUS_FILTER}"
        run_in_bridge "
import httpx, json
r = httpx.get('${BASE}/proposals${QUERY}', timeout=5.0, headers={'Authorization': 'Bearer ${TOKEN}'})
r.raise_for_status()
print(json.dumps(r.json(), indent=2, sort_keys=True))
"
        ;;
    show)
        PID="${1:?proposal_id required}"
        run_in_bridge "
import httpx, json
r = httpx.get('${BASE}/proposals/${PID}', timeout=5.0, headers={'Authorization': 'Bearer ${TOKEN}'})
r.raise_for_status()
print(json.dumps(r.json(), indent=2, sort_keys=True))
"
        ;;
    approve)
        PID="${1:?proposal_id required}"; shift
        REASON=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --reason) REASON="$2"; shift 2 ;;
                *) echo "Unknown option: $1" >&2; exit 1 ;;
            esac
        done
        run_in_bridge "
import httpx, json
r = httpx.post('${BASE}/proposals/${PID}/decide', timeout=5.0, headers={'Authorization': 'Bearer ${TOKEN}'}, json={'decision': 'approve', 'reason': '${REASON}'})
r.raise_for_status()
print(json.dumps(r.json(), indent=2, sort_keys=True))
"
        ;;
    reject)
        PID="${1:?proposal_id required}"; shift
        REASON=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --reason) REASON="$2"; shift 2 ;;
                *) echo "Unknown option: $1" >&2; exit 1 ;;
            esac
        done
        run_in_bridge "
import httpx, json
r = httpx.post('${BASE}/proposals/${PID}/decide', timeout=5.0, headers={'Authorization': 'Bearer ${TOKEN}'}, json={'decision': 'reject', 'reason': '${REASON}'})
r.raise_for_status()
print(json.dumps(r.json(), indent=2, sort_keys=True))
"
        ;;
    execute)
        PID="${1:?proposal_id required}"
        run_in_bridge "
import httpx, json
r = httpx.post('${BASE}/proposals/${PID}/execute', timeout=5.0, headers={'Authorization': 'Bearer ${TOKEN}'})
r.raise_for_status()
print(json.dumps(r.json(), indent=2, sort_keys=True))
"
        ;;
    *)
        echo "Unknown command: $COMMAND" >&2
        usage
        ;;
esac
