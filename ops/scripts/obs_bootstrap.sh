#!/usr/bin/env bash
# urgentry bootstrap (make obs-bootstrap; called by make obs-up).
# Usage: ops/scripts/obs_bootstrap.sh [--prepare] <env file>
#
# 1. Ensures URGENTRY_* bootstrap variables in the env file: missing keys get defaults, empty
#    URGENTRY_BOOTSTRAP_PASSWORD / URGENTRY_BOOTSTRAP_PAT are generated (openssl rand -hex 24).
#    urgentry creates the owner + PAT from them on the first start (no bootstrap-credentials.txt),
#    so make obs-up runs `--prepare` (this step only) BEFORE `docker compose up`.
#    Only these keys and SENTRY_DSN are touched; other lines, order and comments are kept.
#    Before the first change the env file is copied to <env file>.bak.
# 2. Waits for /healthz of the running urgentry, reads the default project and its key via the
#    API (GET /api/0/projects/, /api/0/projects/<org>/<project>/keys/, Bearer PAT; requests run
#    inside the container, the PAT never appears in host argv) and writes
#    SENTRY_DSN=<scheme>://<public_key>@<host of URGENTRY_INTERNAL_URL>/1.
#    sentry_sdk accepts only a numeric project id, urgentry uses the string id `default-project`:
#    URGENTRY_INTERNAL_URL is the internal ingest listener of Caddy (http://caddy:8081), which
#    rewrites /api/1/... to /api/default-project/... (Caddyfile.*). An old value
#    http://urgentry:8080 (first T1 version) is migrated to it; an old DSN is rewritten.
#    For PROJECT_ENVIRONMENT=test SENTRY_DSN is not written (test stack reports nothing).
#    The host of URGENTRY_INTERNAL_URL must be internal (check_internal_url.sh), otherwise
#    the script fails before bootstrap and SENTRY_DSN is not touched. Idempotent.
# Secrets (password, PAT) are never printed.
# Every value written by set_env is also exported to this process: make obs-bootstrap runs
# after `set -a; source .env`, and the shell environment wins over `docker compose --env-file`,
# so a stale (e.g. empty) exported value would otherwise reach docker compose.
set -euo pipefail

prepare_only=0
if [[ ${1:-} == --prepare ]]; then prepare_only=1; shift; fi
env_file=${1:?usage: obs_bootstrap.sh [--prepare] <env file>}
[[ -f $env_file ]] || { echo "ERROR: $env_file not found (cp .env.example .env)" >&2; exit 1; }

backed_up=0
get_env() {  # last value of KEY in env file, quotes stripped
    tr -d '\r' <"$env_file" \
        | sed -nE "s/^[[:space:]]*(export[[:space:]]+)?$1=//p" | tail -n1 \
        | sed -E 's/^[[:space:]]+|[[:space:]]+$//g' | tr -d "\"'"
}
has_key() { grep -qE "^[[:space:]]*(export[[:space:]]+)?$1=" "$env_file"; }
set_env() {  # set_env KEY VALUE: replace existing line(s) or append; value printed never
    local key=$1 value=$2 tmp
    case $value in *'$'* | *'`'* | *'"'* | *"'"* | *$'\n'*)
        echo "ERROR: value of $key must not contain \$, backticks, quotes or newlines" >&2; exit 1;;
    esac
    if [[ $backed_up == 0 ]]; then
        cp -p "$env_file" "$env_file.bak"; backed_up=1
        echo "Backup: $env_file.bak"
    fi
    if has_key "$key"; then
        tmp=$(mktemp "${env_file}.XXXXXX")
        K="$key" V="$value" awk '
            BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"] }
            { line = $0; sub(/^[[:space:]]*(export[[:space:]]+)?/, "", line) }
            index(line, k "=") == 1 { print k "=" v; next }
            { print }
        ' "$env_file" >"$tmp"
        cat "$tmp" >"$env_file"  # keep inode/permissions of the env file
        rm -f "$tmp"
    else
        if ! grep -q '^# --- urgentry bootstrap (make obs-bootstrap) ---$' "$env_file"; then
            printf '\n# --- urgentry bootstrap (make obs-bootstrap) ---\n' >>"$env_file"
        fi
        printf '%s=%s\n' "$key" "$value" >>"$env_file"
    fi
    export "$key=$value"
    echo "Updated $key in $env_file"
}
ensure_default() {  # key missing or empty -> default value
    [[ -n $(get_env "$1") ]] || set_env "$1" "$2"
}
ensure_secret() {  # empty -> random hex; existing value must be hex (URL/env safe)
    local value
    value=$(get_env "$1")
    if [[ -z $value ]]; then
        set_env "$1" "$(openssl rand -hex 24)"
    elif ! [[ $value =~ ^[0-9a-fA-F]{32,}$ ]]; then
        echo "ERROR: $1 in $env_file must be hex (>= 32 chars, openssl rand -hex 24)" >&2; exit 1
    fi
}

ensure_default URGENTRY_BOOTSTRAP_EMAIL admin@urgentry.local
ensure_default URGENTRY_INTERNAL_URL http://caddy:8081
[[ $(get_env URGENTRY_INTERNAL_URL) != http://urgentry:8080 ]] \
    || set_env URGENTRY_INTERNAL_URL http://caddy:8081  # direct urgentry: no numeric project id
# SENTRY_DSN is built from this host: only private IPs / Docker service names (fail early).
bash "$(dirname -- "${BASH_SOURCE[0]}")/check_internal_url.sh" "$(get_env URGENTRY_INTERNAL_URL)" \
    || { echo "ERROR: fix URGENTRY_INTERNAL_URL in $env_file; SENTRY_DSN is not written" >&2; exit 1; }
# Only the Caddy ingest listener maps DSN project id 1 to `default-project` (direct urgentry: 404).
if ! [[ $(get_env URGENTRY_INTERNAL_URL) =~ ^https?://[^/:]+:8081/?$ ]]; then
    echo "WARNING: URGENTRY_INTERNAL_URL should be the Caddy ingest listener http://<host>:8081" \
        "(http://caddy:8081 on one host, http://<VPN IP of VPS 2>:8081 for two hosts);" \
        "sentry_sdk events to other URLs get 404" >&2
fi
ensure_secret URGENTRY_BOOTSTRAP_PASSWORD
ensure_secret URGENTRY_BOOTSTRAP_PAT
(( prepare_only )) && exit 0

compose=(docker compose -f docker-compose.obs.yml --env-file "$env_file")
# GET <path> inside the urgentry container with the PAT from its own environment.
api() {
    # shellcheck disable=SC2016  # expanded by sh inside the container
    "${compose[@]}" exec -T urgentry sh -c \
        'wget -qO- --header "Authorization: Bearer $URGENTRY_BOOTSTRAP_PAT" "http://127.0.0.1:8080$0"' "$1"
}

for _ in $(seq 1 60); do
    if "${compose[@]}" exec -T urgentry wget -qO- http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
        healthy=1; break
    fi
    sleep 2
done
[[ ${healthy:-0} == 1 ]] || { echo "ERROR: urgentry /healthz is not ok (make obs-logs SERVICE=urgentry)" >&2; exit 1; }

projects=$(api /api/0/projects/) \
    || { echo "ERROR: urgentry API /api/0/projects/ failed (URGENTRY_BOOTSTRAP_PAT?)" >&2; exit 1; }
slugs=$(python3 -c '
import json, sys
projects = json.load(sys.stdin)
p = projects[0]
org = p["organization"]  # urgentry v0.2.11: plain slug string; Sentry: object
print(org["slug"] if isinstance(org, dict) else org, p["slug"], p.get("id", ""))
' <<<"$projects") || { echo "ERROR: no project in urgentry API answer" >&2; exit 1; }
read -r org_slug project_slug project_api_id <<<"$slugs"
[[ $org_slug =~ ^[A-Za-z0-9_-]+$ && $project_slug =~ ^[A-Za-z0-9_-]+$ ]] \
    || { echo "ERROR: unexpected organization/project slug from urgentry API" >&2; exit 1; }
keys=$(api "/api/0/projects/$org_slug/$project_slug/keys/") \
    || { echo "ERROR: urgentry API keys/ of $org_slug/$project_slug failed" >&2; exit 1; }
parsed=$(PROJECT_API_ID="$project_api_id" python3 -c '
import json, os, sys
from urllib.parse import urlsplit
keys = [k for k in json.load(sys.stdin) if k.get("isActive", True)]
k = keys[0]
dsn = (k.get("dsn") or {}).get("public") or ""
url = urlsplit(dsn) if dsn else None
key = (url.username if url else None) or k.get("public") or ""
pid = (url.path.rstrip("/").rsplit("/", 1)[-1] if url else "") or str(k.get("projectId") or os.environ["PROJECT_API_ID"])
print(key, pid)
' <<<"$keys") || { echo "ERROR: no key in urgentry API answer" >&2; exit 1; }
read -r public_key project_id <<<"$parsed"
[[ $public_key =~ ^[0-9a-f]{32}$ && $project_id =~ ^[A-Za-z0-9-]+$ ]] \
    || { echo "ERROR: unexpected public key / project id from urgentry API" >&2; exit 1; }
# Caddy maps the DSN project id 1 to exactly this urgentry project (Caddyfile.*, listener :8081).
dsn_project_id=1
[[ $project_id == default-project ]] \
    || { echo "ERROR: urgentry project id is $project_id, Caddy :8081 maps /api/1/ to default-project" >&2; exit 1; }
echo "urgentry bootstrap OK: project=$org_slug/$project_slug project_id=$project_id public_key=${public_key:0:4}…"

if [[ $(get_env PROJECT_ENVIRONMENT) == test ]]; then
    echo "PROJECT_ENVIRONMENT=test: SENTRY_DSN is not written to $env_file"
    exit 0
fi
internal_url=$(get_env URGENTRY_INTERNAL_URL)
[[ $internal_url =~ ^(https?)://([^/]+)/?$ ]] \
    || { echo "ERROR: URGENTRY_INTERNAL_URL must look like http://host:port" >&2; exit 1; }
dsn="${BASH_REMATCH[1]}://${public_key}@${BASH_REMATCH[2]}/${dsn_project_id}"
if [[ $(get_env SENTRY_DSN) == "$dsn" ]]; then
    echo "SENTRY_DSN is up to date"
else
    set_env SENTRY_DSN "$dsn"
    echo "Recreate master_bot/bot_worker to apply SENTRY_DSN (make agent-up)"
fi
