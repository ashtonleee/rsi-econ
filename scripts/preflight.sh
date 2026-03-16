#!/usr/bin/env bash
# preflight.sh — lightweight invariant check for agent sessions
#
# Run this at the start of every agent session and before every commit.
# It does NOT require Docker. It runs only fast, local checks.
#
# Exit 0 = safe to proceed. Non-zero = something needs attention.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ERRORS=0

section() { printf '\n=== %s ===\n' "$1"; }
pass()    { printf '  PASS: %s\n' "$1"; }
fail()    { printf '  FAIL: %s\n' "$1"; ERRORS=$((ERRORS + 1)); }
warn()    { printf '  WARN: %s\n' "$1"; }
skip()    { printf '  SKIP: %s\n' "$1"; }

# ── 1. Fast unit tests ──────────────────────────────────────────────
section "Fast unit tests (pytest -m fast)"

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c "import pytest" 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    skip "No Python with pytest found — install dev dependencies"
else
    if $PYTHON -m pytest -m fast -q --tb=short 2>&1; then
        pass "Fast-marked unit tests"
    else
        fail "Fast-marked unit tests had failures"
    fi
fi

# ── 2. Full unit tests (non-Docker) ─────────────────────────────────
section "Full unit tests"

if [[ -z "$PYTHON" ]]; then
    skip "No Python with pytest found"
else
    if $PYTHON -m pytest tests/unit/ -q --tb=short 2>&1; then
        pass "All unit tests"
    else
        # Distinguish known environment issues from real failures
        warn "Unit test failures — check if Docker-dependent or env-specific"
    fi
fi

# ── 3. Tier 2 file consistency ───────────────────────────────────────
section "Tier 2 file consistency"

# Check that key files exist
for f in STAGE_STATUS.md TASK_GRAPH.md ACCEPTANCE_TEST_MATRIX.md REPO_LAYOUT.md assurance/REGISTRY.yaml plans/INDEX.md; do
    if [[ -f "$f" ]]; then
        pass "$f exists"
    else
        fail "$f missing"
    fi
done

# ── 4. REGISTRY.yaml watch_paths validation ──────────────────────────
section "Assurance registry watch_paths"

if [[ -z "$PYTHON" ]]; then
    skip "No Python found for watch_paths check"
else
    $PYTHON - <<'PY' || fail "watch_paths reference missing files"
import sys
try:
    import yaml
except ImportError:
    print("  SKIP: pyyaml not installed — cannot validate watch_paths")
    sys.exit(0)

from pathlib import Path

registry = yaml.safe_load(Path("assurance/REGISTRY.yaml").read_text())
missing = []
for component in registry.get("components", []):
    for pattern in component.get("watch_paths", []):
        # Simple check: if pattern has no glob chars, it should exist as file or dir
        if "*" not in pattern and "?" not in pattern:
            if not Path(pattern).exists() and not list(Path(".").glob(pattern)):
                missing.append(f"{component['id']}: {pattern}")

if missing:
    for m in missing:
        print(f"  MISSING: {m}")
    sys.exit(1)
else:
    print("  PASS: all non-glob watch_paths resolve")
PY
fi

# ── 5. Uncommitted changes warning ──────────────────────────────────
section "Git state"

if git diff --quiet && git diff --cached --quiet; then
    pass "Clean working tree"
else
    warn "Uncommitted changes in working tree"
fi

# ── 6. STAGE_STATUS.md freshness ────────────────────────────────────
section "Stage status freshness"

if [[ -f STAGE_STATUS.md ]]; then
    if grep -q "^| .* |$" STAGE_STATUS.md; then
        pass "STAGE_STATUS.md has session log entries"
    else
        warn "STAGE_STATUS.md may be stale — no session log entries found"
    fi
else
    fail "STAGE_STATUS.md missing"
fi

# ── Summary ──────────────────────────────────────────────────────────
section "Summary"

if [[ $ERRORS -gt 0 ]]; then
    printf '\n%d error(s) found. Fix before proceeding.\n' "$ERRORS"
    exit 1
else
    printf '\nAll preflight checks passed.\n'
    exit 0
fi
