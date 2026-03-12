# rsi-econ

Stage 1 only: hard boundary proof for a trusted bridge, a separate trusted LiteLLM service, and an untrusted agent sandbox.

This repo does not implement browser automation, approvals, recovery, public-web proxying, or consequential actions yet. It proves the boundary first:

- the untrusted agent only sits on an internal Docker network
- the bridge is the only cross-network hop
- LiteLLM is a separate trusted service
- provider credentials stay on the trusted side
- trusted-side logs live outside the untrusted workspace

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

That command is the primary verification path for Stage 1. It requires the Docker daemon because the boundary proof is container-backed, not host-process-only.

## Docker Workflow

Bring up the stack:

```bash
./scripts/up.sh
```

Inspect bridge health inside the trusted bridge container:

```bash
docker compose exec -T bridge python -c 'import httpx, json; r = httpx.get("http://127.0.0.1:8000/healthz", timeout=5.0); r.raise_for_status(); print(json.dumps(r.json(), indent=2, sort_keys=True))'
```

Inspect bridge status, including LiteLLM connectivity:

```bash
docker compose exec -T bridge python -c 'import httpx, json; r = httpx.get("http://127.0.0.1:8000/status", timeout=5.0); r.raise_for_status(); print(json.dumps(r.json(), indent=2, sort_keys=True))'
```

Make a deterministic LLM request from inside the untrusted sandbox through the bridge path:

```bash
docker compose exec agent python -m untrusted.agent.bridge_client chat --message "stage1 boundary proof"
```

Tail trusted service logs:

```bash
docker compose logs -f bridge litellm
```

Tail the bridge-owned JSONL log:

```bash
tail -f runtime/trusted_state/logs/bridge_events.jsonl
```

Tear the stack down:

```bash
./scripts/down.sh
```

## Host-Process Dev Fallback

This mode is for lightweight app debugging only. It is not the Stage 1 security proof path.

Run LiteLLM in one shell:

```bash
source .venv/bin/activate
OPENAI_API_KEY=stage1-sentinel-provider-key uvicorn trusted.litellm.app:app --host 127.0.0.1 --port 4000
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

## Stage Boundary

- Trusted code lives under `trusted/`.
- Untrusted code and the future agent-owned repo surface live under `untrusted/`.
- Trusted runtime state and Stage 1 bridge logs live under `runtime/trusted_state/`.
- The agent service mounts only `untrusted/agent_workspace/`.
- The agent is on `agent_net` only.
- LiteLLM is on `trusted_net` only.
- The bridge is the only service on both networks.

See `REPO_LAYOUT.md`, `TASK_GRAPH.md`, and `ACCEPTANCE_TEST_MATRIX.md` for the current stage contract.
