#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$ROOT_DIR/runtime/trusted_state"

printf 'Resetting trusted operator state in %s\n' "$STATE_DIR"
printf 'This clears budget, proposals, logs, and checkpoints for local demos.\n'

rm -rf "$STATE_DIR"
mkdir -p "$STATE_DIR"

printf 'Trusted state reset complete.\n'
