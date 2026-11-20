#!/usr/bin/env bash
# Waits until GET /health/ready answers 200 inside master_bot and bot_worker.
#
# Usage: ops/scripts/wait_ready.sh [compose args...]   (e.g. --env-file .env)
#   READY_TIMEOUT   total wait, seconds (default 60)
#   READY_INTERVAL  pause between polls, seconds (default 2)
#
# The probe runs inside the container (python urllib: the image has no curl) on the port of
# OBSERVABILITY_HTTP_PORT from the container environment (default 8080). On timeout prints the
# /health/ready JSON of every service that is not ready and exits 1.
set -uo pipefail

timeout_s=${READY_TIMEOUT:-60}
interval=${READY_INTERVAL:-2}
services=(master_bot bot_worker)

probe='
import os, sys, urllib.error, urllib.request
url = "http://127.0.0.1:" + os.environ.get("OBSERVABILITY_HTTP_PORT", "8080") + "/health/ready"
try:
    with urllib.request.urlopen(url, timeout=5) as r:
        code, body = r.status, r.read().decode()
except urllib.error.HTTPError as e:
    code, body = e.code, e.read().decode()
except Exception as e:
    code, body = 0, "no response: " + type(e).__name__ + ": " + str(e)
print(body)
sys.exit(0 if code == 200 else 1)
'

declare -A last
deadline=$((SECONDS + timeout_s))
while :; do
    pending=()
    for svc in "${services[@]}"; do
        # </dev/null: `timeout` runs the command in a background process group; with stdin on a
        # TTY `docker compose exec` (even -T) reads it, gets SIGTTIN and stops, SIGTERM does not
        # wake it and the outer `timeout` of `make ready` fails with 124 (make: rc 2).
        # -k 5: SIGKILL if the probe ignores SIGTERM.
        if out=$(timeout -k 5 15 docker compose "$@" exec -T "$svc" python -c "$probe" </dev/null 2>&1); then
            last[$svc]=""
        else
            last[$svc]=$out
            pending+=("$svc")
        fi
    done
    if [ ${#pending[@]} -eq 0 ]; then
        echo "ready: ${services[*]} (/health/ready 200)"
        exit 0
    fi
    if [ $SECONDS -ge $deadline ]; then
        break
    fi
    sleep "$interval"
done

echo "ERROR: not ready after ${timeout_s}s: ${pending[*]}" >&2
for svc in "${pending[@]}"; do
    echo "--- $svc /health/ready:" >&2
    echo "${last[$svc]}" >&2
done
exit 1
