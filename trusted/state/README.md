# trusted/state

Placeholder directory for later trusted-side state helpers.

Stage 1 uses the live trusted-state mount at `runtime/trusted_state/` for the bridge-owned JSONL log under `runtime/trusted_state/logs/`.

This is intentionally not the full Stage 2 canonical state system yet. It is minimal trusted-side visibility for containment proof only, and it remains outside the agent-owned mount.
