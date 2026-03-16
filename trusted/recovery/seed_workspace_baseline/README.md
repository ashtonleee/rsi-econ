# agent_workspace

This directory is the only mutable seed repo/workspace for Stage 6B.

- Inside the container, it is mounted at `/workspace/agent`.
- The static runtime and harness code lives under `/app/untrusted`.
- The seed runner may read and write files here, but it must not treat `/app/untrusted` as the self-edit target.
- Trusted recovery resets this workspace back to the seed baseline in `trusted/recovery/seed_workspace_baseline/`.
- Stage 5 adds read-only bridge-mediated web fetch plans under `.seed_plans/`.
- Stage 6A adds read-only bridge-mediated browser render plans under `.seed_plans/`.
- Stage 6A also adds a single-url browser capture packet plan under `.seed_plans/` that writes `research/current_*`.
- Stage 6A also adds deterministic and provider-backed single-source answer packet plans under `.seed_plans/` that write `research/current_answer.md` plus the raw capture artifacts.
- Stage 6B adds one-hop trusted href-follow demo plans plus a one-hop follow answer packet plan under `.seed_plans/` that writes `research/current_follow_*`.

Local validation from inside the agent workspace:

```bash
python -m pytest -q
```

Generated run artifacts go under `run_outputs/` and `research/current_*`, and are ignored by git.
