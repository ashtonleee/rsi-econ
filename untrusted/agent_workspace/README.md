# agent_workspace

This directory is the only mutable seed repo/workspace for Stage 5.

- Inside the container, it is mounted at `/workspace/agent`.
- The static runtime and harness code lives under `/app/untrusted`.
- The seed runner may read and write files here, but it must not treat `/app/untrusted` as the self-edit target.
- Trusted recovery resets this workspace back to the seed baseline in `trusted/recovery/seed_workspace_baseline/`.
- Stage 5 adds read-only bridge-mediated web fetch plans under `.seed_plans/`.

Local validation from inside the agent workspace:

```bash
python -m pytest -q
```

Generated run artifacts go under `run_outputs/` and are ignored by git.
