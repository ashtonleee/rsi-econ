# agent_workspace

This directory is the only mutable seed repo/workspace for Stage 3.

- Inside the container, it is mounted at `/workspace/agent`.
- The static runtime and harness code lives under `/app/untrusted`.
- The seed runner may read and write files here, but it must not treat `/app/untrusted` as the self-edit target.

Local validation from inside the agent workspace:

```bash
python -m pytest -q
```

Generated run artifacts go under `run_outputs/` and are ignored by git.
