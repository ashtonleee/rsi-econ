# untrusted/agent_workspace

This directory represents the future agent-owned repo/workspace mount.

Stage 0 keeps it mostly empty on purpose. The agent container may mount this path, but it must not receive trusted-side state, policy files, secrets, or canonical logs.
