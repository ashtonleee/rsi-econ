# trusted/state

Trusted-side canonical and materialized state helpers.

The live trusted-state mount at `runtime/trusted_state/` now carries:

- canonical append-only events under `runtime/trusted_state/logs/bridge_events.jsonl`
- materialized operational state under `runtime/trusted_state/state/operational_state.json`
- trusted recovery artifacts under `runtime/trusted_state/checkpoints/`

Stage 3 adds a narrow bridge-owned append-only path for untrusted-origin seed-run summaries. Those records are clearly labeled as `reported_origin: untrusted_agent` in canonical events.

Stage 4 adds trusted/operator-origin recovery events:

- `checkpoint_created`
- `checkpoint_restored`
- `workspace_reset`
- `recovery_error`

Stage 5 adds trusted-observed web fetch events:

- `web_fetch`
- `web_fetch_denied`
- `web_fetch_error`

Only trusted services and trusted host-side operator scripts write these files. The agent sees them only through bridge read-only status surfaces, the narrow append-only run-report endpoint, and the safe bridge-owned `/web/fetch` route.
