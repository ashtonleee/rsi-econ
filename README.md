# rsi-econ

Stage 6B only: trusted read-only browser mediation on top of the validated Stage 1-5 boundary, canonical trusted state, recovery, and the local-only seed agent substrate.

For a compact claim-to-evidence audit map, see `assurance/ASSURANCE_CASE.md`, `assurance/REGISTRY.yaml`, and `assurance/RUNBOOK.md`.

This repo does not implement clicks, arbitrary link-following, forms, logins, cookies/sessions, downloads/uploads, approvals, consequential actions, or operator auth on the bridge yet. It now proves:

- the untrusted agent only sits on an internal Docker network
- the bridge is the only cross-network hop
- LiteLLM is a separate trusted service
- provider credentials stay on the trusted side
- canonical event logging lives outside the untrusted workspace
- canonical event actor identity is assigned by trusted routes, not caller-supplied headers
- trusted operational state is materialized outside the untrusted workspace
- mediated LLM usage is budgeted and hard-capped by the bridge
- `/workspace/agent` is the only mutable seed repo/workspace, and the agent container root filesystem is read-only except for `/workspace/agent` and `/tmp`
- `/app/untrusted` stays static runtime/harness code
- the seed runner can use bridge status, bridge chat, local workspace file tools, and a bounded local Python command runner
- trusted checkpoints and reset/restore controls live outside the mutable workspace under `runtime/trusted_state/checkpoints/`
- read-only web fetches go only through `agent -> bridge -> fetcher -> egress`
- the fetch route is fixed to remote `GET` only with an explicit host allowlist, text-only content policy, redirect caps, byte caps, and canonical trusted fetch logging
- read-only browser renders go only through `agent -> bridge -> browser -> egress`
- the browser render route is fixed to a `url`-only request shape with allowlisted navigation, rendered text extraction, page title/metadata extraction, one screenshot, and canonical trusted browser metadata logging
- the browser follow route is fixed to `source_url + target_url`, follows exactly one trusted-extracted allowlisted href, and keeps browser activity read-only and externally visible
- `fetcher` and `browser` no longer dial external destinations directly; the trusted `egress` service remains their outbound connector on `egress_net`, while trusted LiteLLM provider passthrough has its own egress attachment
- the trusted browser runs as a dedicated non-root user with Chromium sandboxing enabled

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run Tests

```bash
./scripts/test.sh
```

That command is the primary verification path for Stage 6B. It requires the Docker daemon because the boundary proof, trusted-state proof, recovery proof, seed-runner proof, mediated web-fetch proof, and mediated browser proof are all container-backed.

## Recommended Confidence Demo

If you want the shortest operator-facing end-to-end demo, use the deterministic Stage 5 fixture fetch path first.

- It proves the trust boundary around read-only external access without browser complexity.
- It writes one human-readable workspace artifact and one trusted event trail outside the sandbox.
- It includes a trusted reset step so you can verify both artifact creation and trusted-side cleanup.

Run this from a clean host shell after installation:

```bash
rm -rf runtime/trusted_state/logs runtime/trusted_state/state runtime/trusted_state/checkpoints
RSI_WEB_ALLOWLIST_HOSTS=allowed.test RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS=allowed.test ./scripts/recovery.sh reset-workspace-to-seed-baseline
RSI_WEB_ALLOWLIST_HOSTS=allowed.test RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS=allowed.test ./scripts/up.sh

docker compose exec -T agent python - <<'PY'
import sys, urllib.request
try:
    urllib.request.urlopen("http://1.1.1.1", timeout=2).read()
except Exception as exc:
    print(type(exc).__name__)
    sys.exit(0)
raise SystemExit("unexpected direct egress success")
PY

docker compose exec -T agent python -m untrusted.agent.seed_runner \
  --task "fetch one allowed page and write a report" \
  --planner scripted \
  --script .seed_plans/stage5_fixture_fetch.json \
  --max-steps 8
```

Inspect these artifacts before reset:

```bash
cat untrusted/agent_workspace/reports/stage5_fixture_fetch_report.txt
python -m json.tool untrusted/agent_workspace/run_outputs/latest_seed_run.json
rg '"event_type": "(status_query|agent_run|web_fetch)"' runtime/trusted_state/logs/bridge_events.jsonl
python - <<'PY'
import json
from pathlib import Path
state = json.loads(Path("runtime/trusted_state/state/operational_state.json").read_text())
print(json.dumps({
    "web": state["web"],
    "recovery": state["recovery"],
    "recent_requests": state["recent_requests"][:6],
}, indent=2))
PY
```

Prove trusted recovery removes only the mutable workspace artifact:

```bash
RSI_WEB_ALLOWLIST_HOSTS=allowed.test RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS=allowed.test ./scripts/recovery.sh reset-workspace-to-seed-baseline
test ! -e untrusted/agent_workspace/reports/stage5_fixture_fetch_report.txt && echo "report removed by trusted reset"
rg '"event_type": "workspace_reset"' runtime/trusted_state/logs/bridge_events.jsonl
python -m json.tool runtime/trusted_state/checkpoints/baselines/seed_workspace_baseline.json

./scripts/down.sh
```

The public-host Stage 5 demo plan at `.seed_plans/stage5_demo_fetch.json` still exists, but it is not the recommended confidence path because it depends on live internet behavior.

## Docker Workflow

Bring up the stack:

```bash
./scripts/up.sh
```

Bring up the deterministic fixture-backed trust demo stack used by the recommended Stage 5 and browser demos:

```bash
RSI_WEB_ALLOWLIST_HOSTS=allowed.test RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS=allowed.test ./scripts/up.sh
```

Inspect bridge health inside the trusted bridge container:

```bash
docker compose exec -T bridge python -c 'import httpx, json; r = httpx.get("http://127.0.0.1:8000/healthz", timeout=5.0); r.raise_for_status(); print(json.dumps(r.json(), indent=2, sort_keys=True))'
```

Inspect bridge status, including LiteLLM connectivity:

```bash
./scripts/status.sh
```

Manual H1 hardening checks:

```bash
docker compose exec -T agent python - <<'PY'
import httpx
r = httpx.get("http://bridge:8000/status", headers={"x-rsi-actor": "operator"}, timeout=10.0)
print(r.status_code)
print(r.headers.get("x-request-id"))
PY
```

```bash
docker compose exec -T agent python - <<'PY'
from pathlib import Path
Path("/workspace/agent/h1_probe.txt").write_text("ok\n", encoding="ascii")
print("workspace_ok")
try:
    Path("/app/untrusted/h1_probe.txt").write_text("bad\n", encoding="ascii")
    print("runtime_write_unexpectedly_succeeded")
except Exception as exc:
    print(type(exc).__name__)
PY
```

```bash
docker compose exec -T browser id
docker compose exec -T browser python - <<'PY'
import httpx, json
r = httpx.get("http://127.0.0.1:8083/healthz", timeout=5.0)
print(json.dumps(r.json(), indent=2, sort_keys=True))
PY
```

Make a deterministic LLM request from inside the untrusted sandbox through the bridge path:

```bash
docker compose exec agent python -m untrusted.agent.bridge_client chat --message "stage1 boundary proof"
```

Query the same read-only trusted status surface from inside the untrusted sandbox:

```bash
docker compose exec agent python -m untrusted.agent.bridge_client status
```

Fetch an allowlisted fixture page through the trusted web path:

```bash
docker compose exec agent python -m untrusted.agent.bridge_client fetch --url http://allowed.test/allowed
```

Render an allowlisted page through the trusted browser path:

```bash
docker compose exec agent python -m untrusted.agent.bridge_client browser-render --url http://allowed.test/browser/rendered
```

Follow one trusted-extracted href through the trusted browser path:

```bash
docker compose exec agent python -m untrusted.agent.bridge_client browser-follow-href --source-url http://allowed.test/browser/follow-source --target-url http://allowed.test/browser/follow-target
```

Those fetch and browser commands assume the stack was started with `RSI_WEB_ALLOWLIST_HOSTS=allowed.test` and `RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS=allowed.test` so the deterministic fixture pages are allowlisted. For the cross-origin Stage 6B fixture path, include `allowed-two.test` in both variables.

Run the one-shot local-only seed runner inside the untrusted sandbox:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "write a local-only run report"
```

Run the deterministic scripted Stage 3 plan used by the integration test:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "write a local-only run report" --planner scripted --script .seed_plans/stage3_local_task.json
```

Run the recommended human-visible Stage 5 fixture fetch demo artifact path:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "fetch one allowed page and write a report" --planner scripted --script .seed_plans/stage5_fixture_fetch.json
```

Inspect the demo report artifact:

```bash
cat untrusted/agent_workspace/reports/stage5_fixture_fetch_report.txt
```

Inspect the untrusted step trace for the same run:

```bash
python -m json.tool untrusted/agent_workspace/run_outputs/latest_seed_run.json
```

Run the human-visible Stage 6A browser render demo artifact path:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "render one allowed page and write a browser report" --planner scripted --script .seed_plans/stage6_browser_demo.json
```

Inspect the Stage 6A browser report and screenshot:

```bash
cat untrusted/agent_workspace/reports/stage6_browser_report.md
```

```bash
file untrusted/agent_workspace/reports/stage6_browser_screenshot.png
```

Run the single-url browser capture packet for one operator-chosen allowlisted page:

```bash
RSI_WEB_ALLOWLIST_HOSTS=docs.python.org ./scripts/up.sh
docker compose exec -T agent python -m untrusted.agent.seed_runner --task "capture one allowlisted page into a research packet" --planner scripted --script .seed_plans/stage6_capture_packet.json --input-url https://docs.python.org/3/library/pathlib.html
```

Inspect the capture packet artifacts:

```bash
cat untrusted/agent_workspace/research/current_capture.md
```

```bash
sed -n '1,120p' untrusted/agent_workspace/research/current_rendered_text.txt
```

```bash
file untrusted/agent_workspace/research/current_screenshot.png
```

Run the deterministic single-source answer packet workflow proof against the fixture-backed page:

```bash
docker compose exec -T agent python -m untrusted.agent.seed_runner --task "What does this page say?" --planner scripted --script .seed_plans/stage6_answer_packet.json --input-url http://allowed.test/browser/rendered
```

Inspect the answer packet and the raw capture side-by-side:

```bash
cat untrusted/agent_workspace/research/current_answer.md
```

```bash
cat untrusted/agent_workspace/research/current_capture.md
```

Run the deterministic one-hop follow answer packet workflow proof against the fixture-backed follow source:

```bash
docker compose exec -T agent python -m untrusted.agent.seed_runner --task "What does the followed page say?" --planner scripted --script .seed_plans/stage6_follow_answer_packet.json --input-url http://allowed.test/browser/follow-source --follow-target-url http://allowed.test/browser/follow-target
```

This remains read-only and narrow: the trusted browser renders the source page, follows exactly one operator-chosen trusted-extracted href, and writes `research/current_follow_*` artifacts from the followed page only.

Inspect the one-hop follow answer packet and raw follow capture:

```bash
cat untrusted/agent_workspace/research/current_follow_answer.md
```

```bash
cat untrusted/agent_workspace/research/current_follow_capture.md
```

Run the provider-backed single-source answer packet workflow after opting into trusted provider mode:

```bash
./scripts/down.sh
./scripts/provider.sh init
$EDITOR .env.provider.local
./scripts/provider.sh up
./scripts/provider.sh smoke --model openai/gpt-4.1-mini
./scripts/provider.sh answer-packet --model openai/gpt-4.1-mini --task "How do I iterate directory entries with pathlib.Path and what caveat matters here?" --input-url https://docs.python.org/3/library/pathlib.html
```

Run any existing scripted Stage 6 seed plan through the same trusted provider mode without per-command patching:

```bash
./scripts/provider.sh seed-run --script .seed_plans/stage6_follow_answer_packet.json --model openai/gpt-4.1-mini --task "What does the followed page say?" --input-url http://allowed.test/browser/follow-source --follow-target-url http://allowed.test/browser/follow-target --max-steps 10
```

For the operator-facing wrapper around that same one-hop read-only follow path:

```bash
./scripts/provider.sh follow-answer-packet --model openai/gpt-4.1-mini --task "What does the followed page say?" --input-url http://allowed.test/browser/follow-source --follow-target-url http://allowed.test/browser/follow-target
```

The trusted-side provider env file is `.env.provider.local` by default and must stay untracked. The helper reads only these trusted-side variables:

```bash
OPENAI_API_KEY=replace-with-real-provider-key
RSI_OPENAI_BASE_URL=https://api.openai.com/v1
RSI_PROVIDER_SMOKE_MODEL=gpt-4.1-mini
RSI_PROVIDER_ANSWER_MODEL=gpt-4.1-mini
RSI_WEB_ALLOWLIST_HOSTS=docs.python.org
RSI_LLM_BUDGET_TOKEN_CAP=4000
```

Only `OPENAI_API_KEY` is required for `provider_passthrough`. The other variables are optional:

- `RSI_OPENAI_BASE_URL`: trusted-side OpenAI-compatible base URL override
- `RSI_PROVIDER_SMOKE_MODEL`: default model used by `./scripts/provider.sh smoke`
- `RSI_PROVIDER_ANSWER_MODEL`: default model used by `./scripts/provider.sh seed-run`, `answer-packet`, and `follow-answer-packet`
- `RSI_WEB_ALLOWLIST_HOSTS`: needed for live browser/fetch runs against allowlisted hosts
- `RSI_LLM_BUDGET_TOKEN_CAP`: useful when the provider-backed run needs a larger token budget

For OpenRouter-backed runs, keep the same commands and set these in `.env.provider.local`:

```bash
RSI_OPENAI_BASE_URL=https://openrouter.ai/api/v1
RSI_PROVIDER_SMOKE_MODEL=openai/gpt-4.1-mini
RSI_PROVIDER_ANSWER_MODEL=openai/gpt-4.1-mini
```

Inspect the provider-backed answer packet:

```bash
cat untrusted/agent_workspace/research/current_answer.md
```

The saved `llm_model=` field now comes from the exact model identifier returned by the trusted bridge chat path, not just the requested model string. That keeps OpenRouter-style/versioned provider model IDs visible in the operator-facing artifact.

The same returned-model provenance now applies to `research/current_follow_answer.md` when you run the one-hop follow answer packet through `./scripts/provider.sh seed-run` or `./scripts/provider.sh follow-answer-packet`.

For a fixture-backed automated proof of the same passthrough path without using a live provider, run:

```bash
python -m pytest -q tests/integration/test_provider_passthrough_compose.py
```

Run the human-visible Stage 6B browser follow demo artifact path:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "follow one safe href and write a browser report" --planner scripted --script .seed_plans/stage6b_browser_follow_demo.json
```

Inspect the Stage 6B browser follow report and screenshot:

```bash
cat untrusted/agent_workspace/reports/stage6b_browser_follow_report.md
```

```bash
file untrusted/agent_workspace/reports/stage6b_browser_follow_screenshot.png
```

Tail trusted service logs:

```bash
docker compose logs -f bridge browser fetcher egress litellm
```

Tail the canonical append-only event log:

```bash
tail -f runtime/trusted_state/logs/bridge_events.jsonl
```

Inspect only the canonical Stage 5 fetch events:

```bash
rg '"event_type": "web_fetch|web_fetch_denied|web_fetch_error"' runtime/trusted_state/logs/bridge_events.jsonl
```

Inspect only the canonical Stage 6A/6B browser events:

```bash
rg '"event_type": "browser_render|browser_render_denied|browser_render_error|browser_follow_href|browser_follow_href_denied|browser_follow_href_error"' runtime/trusted_state/logs/bridge_events.jsonl
```

Inspect the materialized operational state snapshot, including the `web` and `browser` sections:

```bash
python -m json.tool runtime/trusted_state/state/operational_state.json
```

List trusted checkpoints from the operator side:

```bash
./scripts/recovery.sh list-checkpoints
```

Create a trusted checkpoint of the mutable workspace:

```bash
./scripts/recovery.sh create-checkpoint --label "before-local-edit"
```

Restore a trusted checkpoint by ID:

```bash
./scripts/recovery.sh restore-checkpoint ckpt-...
```

Reset the mutable workspace back to the trusted seed baseline:

```bash
./scripts/recovery.sh reset-workspace-to-seed-baseline
```

Inspect the mutable seed workspace on the host:

```bash
find untrusted/agent_workspace -maxdepth 3 -type f | sort
```

Tear the stack down:

```bash
./scripts/down.sh
```

## Host-Process Dev Fallback

This mode is for lightweight app debugging only. It is not the Stage 6B trust proof path.

Run LiteLLM in one shell:

```bash
source .venv/bin/activate
OPENAI_API_KEY=stage1-sentinel-provider-key uvicorn trusted.litellm.app:app --host 127.0.0.1 --port 4000
```

For trusted provider passthrough in this fallback mode, opt in explicitly:

```bash
source .venv/bin/activate
RSI_LITELLM_RESPONSE_MODE=provider_passthrough OPENAI_API_KEY=your-real-provider-key uvicorn trusted.litellm.app:app --host 127.0.0.1 --port 4000
```

Run the bridge in a second shell:

```bash
source .venv/bin/activate
RSI_LITELLM_URL=http://127.0.0.1:4000 \
RSI_AGENT_URL=http://127.0.0.1:8001 \
uvicorn trusted.bridge.app:app --host 127.0.0.1 --port 8000
```

Run the untrusted agent in a third shell:

```bash
source .venv/bin/activate
RSI_BRIDGE_URL=http://127.0.0.1:8000 uvicorn untrusted.agent.app:app --host 127.0.0.1 --port 8001
```

## Stage 3 Workspace Model

- Mutable self-edit target: `untrusted/agent_workspace/`, mounted at `/workspace/agent`
- Static runtime/harness code: `untrusted/agent/`, installed in the container under `/app/untrusted`
- Default local validation surface: `python -m pytest -q` from inside `/workspace/agent`
- Default bounded command surface: Python only, via the seed runner’s local command runner

The seed runner may modify files only in `/workspace/agent`. In the Docker-backed proof path, `/app/untrusted` is on the agent container's read-only root filesystem, `/tmp` is the only general scratch space, and the agent still does not gain any direct path to trusted files.

## Stage 2 State Model

- Canonical truth: `runtime/trusted_state/logs/bridge_events.jsonl`
- Derived operational state: `runtime/trusted_state/state/operational_state.json`
- Budget/accounting approximation: deterministic token usage from the local mock LiteLLM path
- Hard cap enforcement: the bridge denies mediated LLM calls when remaining budget cannot cover the next deterministic request

The agent may query trusted status through the bridge and may append untrusted-origin run summaries through the narrow Stage 3 bridge event path, but it still cannot mount or mutate trusted state files directly. Caller-declared headers are not canonical identity; the bridge assigns actor identity server-side per route.

Trusted state persists across bridge restarts because `runtime/trusted_state/` is host-backed. For a clean local development reset, bring the stack down first and then delete:

```bash
rm -f runtime/trusted_state/logs/bridge_events.jsonl
rm -f runtime/trusted_state/state/operational_state.json
rm -rf runtime/trusted_state/checkpoints
```

## Stage 4 Recovery Model

- Trusted recovery state lives under `runtime/trusted_state/checkpoints/`.
- The trusted seed baseline lives under `trusted/recovery/seed_workspace_baseline/`.
- `reset-workspace-to-seed-baseline` means rewrite `untrusted/agent_workspace/` to exactly the trusted seed baseline tree.
- Checkpoint creation, restore, and reset are host-side operator controls only. There are no mutating recovery bridge routes in Stage 4.
- Recovery actions are canonical trusted events with durable request IDs and trace IDs.
- Bridge `/status` exposes read-only recovery state derived from the canonical log and the trusted checkpoint store.

## Stage 5 Web Model

- New topology edge: `fetcher` is a separate trusted service on `trusted_net` only.
- New trusted outbound connector: `egress` is the read-only web connector on `trusted_net` + `egress_net`, while trusted LiteLLM provider passthrough also attaches to `egress_net` for provider reachability.
- `bridge` stays off `egress_net`; it calls `fetcher` over `trusted_net`.
- `fetcher` stays off `egress_net`; it calls `egress` over `trusted_net`.
- `agent` stays on `agent_net` only and still has no direct public/provider/LiteLLM/fetcher path.
- The bridge exposes one new safe route: `POST /web/fetch`.
- Remote fetches are fixed to `GET` only.
- Allowed by default: allowlisted `http`/`https` URLs with default ports and text-oriented content types.
- Forbidden in Stage 5: non-HTTP(S), fragments, userinfo, arbitrary ports, cookies, sessions, auth headers, binary/media downloads, and browser/JS behavior.
- Canonical fetch events record trusted-observed metadata only. Raw fetched bodies stay out of canonical trusted state.

## Stage 6 Browser Model

- New topology edge: `browser` is a separate trusted service on `trusted_net` only.
- The same trusted `egress` service remains the browser mediation component on `egress_net`; trusted LiteLLM provider passthrough also needs trusted-side external reachability.
- `bridge` stays off `egress_net`; it calls `browser` over `trusted_net`.
- `browser` stays off `egress_net`; top-level destination access is mediated through `egress`.
- `agent` stays on `agent_net` only and still has no direct public/provider/LiteLLM/fetcher/browser path.
- The bridge exposes one new safe route: `POST /web/browser/render`.
- The bridge also exposes one safe follow route: `POST /web/browser/follow-href`.
- Browser render requests are fixed to `url` only.
- Browser follow requests are fixed to `source_url + target_url`, where `target_url` must come from a trusted-extracted bounded `followable_links` list.
- Allowed in Stage 6A/6B: allowlisted navigation, rendered text extraction, page title extraction, small metadata extraction, one screenshot, and one-hop trusted href-following.
- Forbidden in Stage 6A/6B: clicks, arbitrary link-following, forms, logins, cookies/sessions, uploads/downloads, arbitrary waits, arbitrary selectors, agent-supplied JS, page/session handles, and multi-hop browsing.
- The trusted browser runs as a dedicated non-root user and launches Chromium without `--no-sandbox` or `--disable-setuid-sandbox`.
- Canonical browser events record trusted-observed metadata and hashes only. Raw rendered text and screenshot bytes stay out of canonical trusted state.

## Stage Boundary

- Trusted code lives under `trusted/`.
- Static untrusted runtime code lives under `untrusted/agent/`.
- The mutable seed repo/workspace lives under `untrusted/agent_workspace/`.
- Trusted runtime state, canonical events, materialized operational state, and checkpoints live under `runtime/trusted_state/`.
- The agent service mounts only `untrusted/agent_workspace/`.
- The agent is on `agent_net` only.
- LiteLLM is on `trusted_net` + `egress_net`.
- The bridge is on `agent_net` + `trusted_net`.
- The fetcher is on `trusted_net` only.
- The browser is on `trusted_net` only.
- The egress service is on `trusted_net` + `egress_net`.
- The web fixture used by deterministic tests is on `egress_net` only.

See `REPO_LAYOUT.md`, `TASK_GRAPH.md`, and `ACCEPTANCE_TEST_MATRIX.md` for the current stage contract.
