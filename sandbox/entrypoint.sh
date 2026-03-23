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

# Git repo is managed by the bridge — no git init/clone in sandbox

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec python /opt/supervisor/supervisor.py
