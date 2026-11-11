#!/usr/bin/env bash
# GlitchTip bootstrap (make obs-bootstrap; called by make obs-up).
# Usage: ops/scripts/obs_bootstrap.sh <env file>
#
# 1. Ensures GLITCHTIP_* bootstrap variables in the env file: missing keys get defaults,
#    empty GLITCHTIP_ADMIN_PASSWORD (openssl rand -hex 24) / GLITCHTIP_PROJECT_PUBLIC_KEY (UUID4)
#    are generated. Only these keys and SENTRY_DSN are touched; other lines, order and comments
#    are kept. Before the first change the env file is copied to <env file>.bak.
# 2. Runs the one-shot service glitchtip-bootstrap (docker-compose.obs.yml, profile bootstrap),
#    which creates superuser/organization/team/project/key idempotently.
# 3. Parses the marker GLITCHTIP_BOOTSTRAP_OK and writes
#    SENTRY_DSN=<scheme>://<public_key>@<host of GLITCHTIP_INTERNAL_URL>/<project_id>.
#    For PROJECT_ENVIRONMENT=test SENTRY_DSN is not written (test stack reports nothing).
# Secrets (password, DSN) are never printed.
set -euo pipefail

env_file=${1:?usage: obs_bootstrap.sh <env file>}
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
        if ! grep -q '^# --- GlitchTip bootstrap (make obs-bootstrap) ---$' "$env_file"; then
            printf '\n# --- GlitchTip bootstrap (make obs-bootstrap) ---\n' >>"$env_file"
        fi
        printf '%s=%s\n' "$key" "$value" >>"$env_file"
    fi
    echo "Updated $key in $env_file"
}
ensure_default() {  # key missing or empty -> default value
    [[ -n $(get_env "$1") ]] || set_env "$1" "$2"
}

ensure_default GLITCHTIP_ADMIN_EMAIL admin@glitchtip.local
ensure_default GLITCHTIP_ORG_SLUG tgshopplatform
ensure_default GLITCHTIP_ORG_NAME TgShopPlatform
ensure_default GLITCHTIP_TEAM_SLUG platform
ensure_default GLITCHTIP_PROJECT_NAME tg-shop-platform
ensure_default GLITCHTIP_PROJECT_PLATFORM python
ensure_default GLITCHTIP_INTERNAL_URL http://glitchtip-web:8000
[[ -n $(get_env GLITCHTIP_ADMIN_PASSWORD) ]] \
    || set_env GLITCHTIP_ADMIN_PASSWORD "$(openssl rand -hex 24)"
[[ -n $(get_env GLITCHTIP_PROJECT_PUBLIC_KEY) ]] \
    || set_env GLITCHTIP_PROJECT_PUBLIC_KEY "$(python3 -c 'import uuid; print(uuid.uuid4())')"

out=$(docker compose -f docker-compose.obs.yml --env-file "$env_file" --profile bootstrap \
    run --rm -T glitchtip-bootstrap 2>&1) || {
    echo "$out" | grep -v -i 'password' >&2
    echo "ERROR: glitchtip-bootstrap failed" >&2
    exit 1
}
echo "$out" | grep '^glitchtip-bootstrap:' || true
marker=$(echo "$out" | grep -E '^GLITCHTIP_BOOTSTRAP_OK project_id=[0-9]+ public_key=[0-9a-f]{32}$' | tail -n1)
[[ -n $marker ]] || { echo "ERROR: bootstrap marker not found" >&2; exit 1; }
project_id=$(sed -E 's/.*project_id=([0-9]+).*/\1/' <<<"$marker")
public_key=$(sed -E 's/.*public_key=([0-9a-f]+).*/\1/' <<<"$marker")
echo "GlitchTip bootstrap OK: project_id=$project_id public_key=${public_key:0:4}…"

if [[ $(get_env PROJECT_ENVIRONMENT) == test ]]; then
    echo "PROJECT_ENVIRONMENT=test: SENTRY_DSN is not written to $env_file"
    exit 0
fi
internal_url=$(get_env GLITCHTIP_INTERNAL_URL)
[[ $internal_url =~ ^(https?)://([^/]+)/?$ ]] \
    || { echo "ERROR: GLITCHTIP_INTERNAL_URL must look like http://host:port" >&2; exit 1; }
dsn="${BASH_REMATCH[1]}://${public_key}@${BASH_REMATCH[2]}/${project_id}"
if [[ $(get_env SENTRY_DSN) == "$dsn" ]]; then
    echo "SENTRY_DSN is up to date"
else
    set_env SENTRY_DSN "$dsn"
    echo "Recreate master_bot/bot_worker to apply SENTRY_DSN (make agent-up)"
fi
