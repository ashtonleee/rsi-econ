#!/bin/bash
set -e

for _ in $(seq 1 30); do
    if [ -f /usr/local/share/ca-certificates/rsi-egress.crt ]; then
        update-ca-certificates >/dev/null 2>&1
        echo "[entrypoint] CA cert installed" >&2
        break
    fi
    sleep 1
done

if [ ! -f /usr/local/share/ca-certificates/rsi-egress.crt ]; then
    echo "[entrypoint] WARNING: no proxy CA cert found" >&2
fi

git config --global user.email "agent@rsi-sandbox"
git config --global user.name "rsi-agent"

# Workspace initialization
if [ ! -d /workspace/agent/.git ]; then
    if [ -n "${GIT_REMOTE_URL:-}" ]; then
        # Clone from remote and create a session branch
        echo "[entrypoint] cloning workspace from $GIT_REMOTE_URL" >&2
        git clone "$GIT_REMOTE_URL" /workspace/agent.tmp
        # Move .git into workspace (seed files are already bind-mounted)
        mv /workspace/agent.tmp/.git /workspace/agent/.git
        rm -rf /workspace/agent.tmp
        SESSION_BRANCH="session/$(date +%Y%m%d-%H%M%S)"
        cd /workspace/agent
        git checkout -b "$SESSION_BRANCH"
        echo "[entrypoint] created session branch: $SESSION_BRANCH" >&2
        cd - >/dev/null 2>/dev/null || true
    else
        echo "[entrypoint] no remote configured, supervisor will git init" >&2
    fi
else
    echo "[entrypoint] resuming existing session" >&2
    # Set up remote if configured and not already present
    if [ -n "${GIT_REMOTE_URL:-}" ]; then
        cd /workspace/agent 2>/dev/null || true
        if ! git remote get-url origin >/dev/null 2>&1; then
            git remote add origin "$GIT_REMOTE_URL"
            echo "[entrypoint] git remote added: $GIT_REMOTE_URL" >&2
        fi
        cd - >/dev/null 2>/dev/null || true
    fi
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec python /opt/supervisor/supervisor.py
