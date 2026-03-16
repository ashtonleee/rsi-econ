#!/usr/bin/env bash
# staleness.sh — detect assurance registry staleness from git diff
#
# Compares the current worktree against the reviewed_commit in REGISTRY.yaml
# and reports which assurance components have stale watch_paths.
#
# Usage:
#   ./scripts/staleness.sh              # compare against reviewed_commit
#   ./scripts/staleness.sh <commit>     # compare against a specific commit
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REGISTRY="assurance/REGISTRY.yaml"

if [[ ! -f "$REGISTRY" ]]; then
    echo "ERROR: $REGISTRY not found" >&2
    exit 1
fi

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python not found" >&2
    exit 1
fi

# Extract reviewed_commit from REGISTRY.yaml
REVIEWED_COMMIT="${1:-}"
if [[ -z "$REVIEWED_COMMIT" ]]; then
    REVIEWED_COMMIT=$($PYTHON -c "
import sys
try:
    import yaml
except ImportError:
    # Fallback: grep for reviewed_commit line
    import re
    text = open('$REGISTRY').read()
    m = re.search(r'^reviewed_commit:\s*(\S+)', text, re.MULTILINE)
    print(m.group(1) if m else '')
    sys.exit(0)
d = yaml.safe_load(open('$REGISTRY'))
print(d.get('reviewed_commit', ''))
")
fi

if [[ -z "$REVIEWED_COMMIT" ]]; then
    echo "ERROR: Could not extract reviewed_commit from $REGISTRY" >&2
    exit 1
fi

# Get list of changed files since reviewed_commit
CHANGED_FILES=$(git diff --name-only "$REVIEWED_COMMIT" HEAD 2>/dev/null || true)
UNCOMMITTED=$(git diff --name-only 2>/dev/null || true)
ALL_CHANGED=$(printf '%s\n%s' "$CHANGED_FILES" "$UNCOMMITTED" | sort -u | grep -v '^$' || true)

if [[ -z "$ALL_CHANGED" ]]; then
    echo "No files changed since reviewed_commit ($REVIEWED_COMMIT). All components fresh."
    exit 0
fi

# Check each component's watch_paths against changed files
$PYTHON - "$REVIEWED_COMMIT" <<'PY'
import fnmatch
import sys

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed — run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from pathlib import Path

reviewed_commit = sys.argv[1]
registry = yaml.safe_load(Path("assurance/REGISTRY.yaml").read_text())

# Read changed files from stdin-like approach — we'll re-read from git
import subprocess
result = subprocess.run(
    ["git", "diff", "--name-only", reviewed_commit, "HEAD"],
    capture_output=True, text=True
)
changed = set(result.stdout.strip().splitlines()) if result.stdout.strip() else set()

# Also include uncommitted
result2 = subprocess.run(
    ["git", "diff", "--name-only"],
    capture_output=True, text=True
)
if result2.stdout.strip():
    changed |= set(result2.stdout.strip().splitlines())

# Strip rsi-econ/ prefix if present (git may include it from parent)
cleaned = set()
for f in changed:
    cleaned.add(f.removeprefix("rsi-econ/"))
changed = cleaned

stale_components = []

for component in registry.get("components", []):
    cid = component["id"]
    watch_paths = component.get("watch_paths", [])
    hits = []
    for pattern in watch_paths:
        for cf in changed:
            if fnmatch.fnmatch(cf, pattern):
                hits.append(cf)
    if hits:
        stale_components.append((cid, component.get("title", cid), hits))

print(f"Reviewed commit: {reviewed_commit}")
print(f"Changed files since then: {len(changed)}")
print()

if not stale_components:
    print("All assurance components are FRESH.")
else:
    print(f"{len(stale_components)} component(s) are STALE:\n")
    for cid, title, hits in stale_components:
        print(f"  {cid} ({title})")
        for h in sorted(set(hits))[:5]:
            print(f"    - {h}")
        if len(hits) > 5:
            print(f"    ... and {len(hits) - 5} more")
        print()

    print("Action: Review these components per assurance/RUNBOOK.md before")
    print("updating reviewed_commit in REGISTRY.yaml.")

sys.exit(1 if stale_components else 0)
PY
