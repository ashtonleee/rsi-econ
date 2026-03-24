#!/bin/bash
set -e

# Wait for proxy CA cert (up to 30s)
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

# Git config for any local git operations (agent might use git directly)
git config --global user.email "agent@rsi-sandbox" 2>/dev/null
git config --global user.name "rsi-agent" 2>/dev/null

# --- Seed initialization ---
# On fresh start (empty workspace), get canonical seed from GitHub or fallback.
# On resume (workspace has files), skip — preserves agent's self-edited code.
if [ -z "$(ls -A /workspace/agent/ 2>/dev/null)" ]; then
    SEED_LOADED=false
    # Try GitHub clone first (may fail if no network access from sandbox)
    if [ -n "$GIT_REMOTE_URL" ] && [ "$SEED_LOADED" = "false" ]; then
        echo "[entrypoint] trying to clone seed from $GIT_REMOTE_URL" >&2
        if git clone "$GIT_REMOTE_URL" /tmp/seed-clone 2>/dev/null; then
            cp -a /tmp/seed-clone/. /workspace/agent/
            rm -rf /tmp/seed-clone
            echo "[entrypoint] seed cloned from GitHub" >&2
            SEED_LOADED=true
        else
            echo "[entrypoint] GitHub clone failed (expected in sandbox), using fallback" >&2
            rm -rf /tmp/seed-clone
        fi
    fi
    # Fallback: copy from baked-in seed
    if [ "$SEED_LOADED" = "false" ] && [ -d /opt/seed-fallback ] && [ -n "$(ls -A /opt/seed-fallback/ 2>/dev/null)" ]; then
        echo "[entrypoint] using local seed fallback" >&2
        cp -r /opt/seed-fallback/* /workspace/agent/
        cp /opt/seed-fallback/.gitignore /workspace/agent/ 2>/dev/null || true
        SEED_LOADED=true
    fi
    if [ "$SEED_LOADED" = "false" ]; then
        echo "[entrypoint] WARNING: no seed source available" >&2
    fi
else
    echo "[entrypoint] workspace exists, resuming session" >&2
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec python /opt/supervisor/supervisor.py
