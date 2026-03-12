# trusted/state

Trusted-side canonical and materialized state helpers.

Stage 2 uses the live trusted-state mount at `runtime/trusted_state/` for:

- canonical append-only events under `runtime/trusted_state/logs/bridge_events.jsonl`
- materialized operational state under `runtime/trusted_state/state/operational_state.json`

Stage 3 adds a narrow bridge-owned append-only path for untrusted-origin seed-run summaries. Those records are clearly labeled as `reported_origin: untrusted_agent` in canonical events.

Only trusted services write these files. The agent sees them only through bridge read-only status surfaces and the narrow append-only run-report endpoint.
