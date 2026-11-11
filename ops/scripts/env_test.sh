#!/usr/bin/env bash
# Generates an isolated test env file from the main (dev) env file.
# Usage: ops/scripts/env_test.sh <source .env> <target .env.test>
#
# Rules:
#   source must be a dev file: PROJECT_DATA_DIR absolute and PROJECT_DATA_DIR/PROJECT_NAME
#   must not already end with "_test" (otherwise test and dev would share data);
#   POSTGRES_DB must be set and must not end with "_test" either;
#   PROJECT_ENVIRONMENT=test; PROJECT_NAME/PROJECT_DATA_DIR/POSTGRES_DB get a "_test" suffix
#   (integration fixtures refuse any database whose name does not end with "_test");
#   REDIS_DB is forced to 15 so the test cache never shares keys with the dev cache;
#   *_DATA_DIR are written as literal paths under the test PROJECT_DATA_DIR (order-independent);
#   *_PORT_HOST are shifted by PORT_OFFSET (default 20000), result must be within 1..65535;
#   MASTER_BOT_TOKEN -> mock; ENCRYPTION_KEYS_OLD -> empty;
#   ENCRYPTION_KEY is kept from an existing target file if valid, otherwise a new one is generated;
#   GLITCHTIP_ADMIN_PASSWORD / GLITCHTIP_PROJECT_PUBLIC_KEY of the test obs stack are own values
#   (kept from an existing target file, otherwise generated); SENTRY_DSN, SENTRY_RELEASE -> empty.
# Lines are normalized: CR, leading whitespace and "export " are stripped.
set -euo pipefail

src=${1:?usage: env_test.sh <source> <target>}
dst=${2:?usage: env_test.sh <source> <target>}
port_offset=${PORT_OFFSET:-20000}

[[ -f $src ]] || { echo "ERROR: $src not found (cp .env.example .env)" >&2; exit 1; }
if [[ -e $dst && $(realpath "$src") == "$(realpath "$dst")" ]]; then
    echo "ERROR: source and target env files are the same file ($src)" >&2
    exit 1
fi

gen_key() {
    uv run --quiet python -c \
        'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
}
valid_key() {
    uv run --quiet python -c \
        'import sys; from cryptography.fernet import Fernet; Fernet(sys.argv[1].encode())' \
        "$1" >/dev/null 2>&1
}

dst_value() {  # last value of KEY in an existing target file
    [[ -f $dst ]] || return 0
    tr -d '\r' <"$dst" \
        | sed -nE "s/^[[:space:]]*(export[[:space:]]+)?$1=//p" \
        | tail -n1 | tr -d "\"'"
}

key=$(dst_value ENCRYPTION_KEY)
# Own GlitchTip bootstrap secrets for the test obs stack (stable across regenerations,
# otherwise the existing test superuser would not match the password).
gt_password=$(dst_value GLITCHTIP_ADMIN_PASSWORD)
[[ $gt_password =~ ^[0-9a-f]{48}$ ]] || gt_password=$(openssl rand -hex 24)
gt_public_key=$(dst_value GLITCHTIP_PROJECT_PUBLIC_KEY)
[[ $gt_public_key =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
    || gt_public_key=$(python3 -c 'import uuid; print(uuid.uuid4())')
key_msg="Keeping existing ENCRYPTION_KEY from $dst"
if [[ -z $key ]] || ! valid_key "$key"; then
    key=$(gen_key)
    key_msg="Generated new ENCRYPTION_KEY for $dst"
fi

tmp=$(mktemp "${dst}.XXXXXX")
trap 'rm -f "$tmp"' EXIT

awk -v key="$key" -v tenv="$dst" -v off="$port_offset" -v gtpw="$gt_password" -v gtpk="$gt_public_key" '
function norm(s) { gsub(/\r/, "", s); sub(/^[[:space:]]+/, "", s); sub(/^export[[:space:]]+/, "", s); return s }
function unq(s) { if (s ~ /^".*"$/ || s ~ /^\047.*\047$/) return substr(s, 2, length(s) - 2); return s }
function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
function strip(s) { s = trim(unq(trim(s))); sub(/\/+$/, "", s); return s }
function fail(msg) { print "ERROR: " msg > "/dev/stderr"; err = 1; exit 2 }
function port(k, v,  p, r) {
    p = unq(v); if (p == "") p = pd[k]
    if (p !~ /^[0-9]+$/) fail(k "=" p " is not a number")
    r = p + off
    if (p < 1 || r > 65535) fail(k "=" p " + offset " off " = " r " is outside 1..65535")
    return r
}
BEGIN {
    dd["POSTGRES_DATA_DIR"] = "db"; dd["REDIS_DATA_DIR"] = "redis"; dd["RABBITMQ_DATA_DIR"] = "rabbitmq"
    dd["MINIO_DATA_DIR"] = "minio"; dd["LOGS_DATA_DIR"] = "logs"
    # observability stack (docker-compose.obs.yml)
    dd["OBS_POSTGRES_DATA_DIR"] = "obs/db"; dd["OBS_REDIS_DATA_DIR"] = "obs/redis"
    dd["OBS_GRAFANA_DATA_DIR"] = "obs/grafana"; dd["OBS_PROMETHEUS_DATA_DIR"] = "obs/prometheus"
    dd["OBS_LOKI_DATA_DIR"] = "obs/loki"; dd["OBS_ALERTMANAGER_DATA_DIR"] = "obs/alertmanager"
    dd["OBS_CADDY_DATA_DIR"] = "obs/caddy"
    dd["ALLOY_DATA_DIR"] = "alloy"  # collection agent (docker-compose.yml, profile observability)
    pd["POSTGRES_PORT_HOST"] = 5432; pd["REDIS_PORT_HOST"] = 6379; pd["RABBITMQ_PORT_HOST"] = 5672
    pd["RABBITMQ_MGMT_PORT_HOST"] = 15672; pd["MINIO_API_PORT_HOST"] = 9000; pd["MINIO_CONSOLE_PORT_HOST"] = 9001
    pd["GRAFANA_PORT_HOST"] = 3000; pd["LOKI_PORT_HOST"] = 3100; pd["PROMETHEUS_PORT_HOST"] = 9090
    pd["ALERTMANAGER_PORT_HOST"] = 9093; pd["GLITCHTIP_PORT_HOST"] = 8000
    pd["CADDY_HTTP_PORT_HOST"] = 80; pd["CADDY_HTTPS_PORT_HOST"] = 443
    pd["ALLOY_PORT_HOST"] = 12345
}
# Pass 1: collect and validate PROJECT_DATA_DIR / PROJECT_NAME.
FNR == NR {
    line = norm($0)
    if (line ~ /^#/ || index(line, "=") == 0) next
    k = substr(line, 1, index(line, "=") - 1); v = strip(substr(line, index(line, "=") + 1))
    if (k == "PROJECT_DATA_DIR") { pdir = v; has_pdir = 1 }
    if (k == "PROJECT_NAME") pname = v
    if (k == "POSTGRES_DB") { pgdb = v; has_pgdb = 1 }
    next
}
FNR == 1 {
    if (!has_pdir || pdir == "") fail("PROJECT_DATA_DIR is empty or missing in source env")
    if (pdir !~ /^\// || pdir ~ /\$/) fail("PROJECT_DATA_DIR must be an absolute literal path, got: " pdir)
    if (pdir ~ /_test$/) fail("PROJECT_DATA_DIR already ends with _test; source must be the dev env file")
    if (pname ~ /_test$/) fail("PROJECT_NAME already ends with _test; source must be the dev env file")
    if (!has_pgdb || pgdb == "") fail("POSTGRES_DB is empty or missing in source env")
    if (pgdb ~ /_test$/) fail("POSTGRES_DB already ends with _test; source must be the dev env file")
    tdir = pdir "_test"
}
# Pass 2: write the test env.
{
    line = norm($0)
    if (line == "" || line ~ /^#/ || index(line, "=") == 0) { print line; next }
    k = substr(line, 1, index(line, "=") - 1); v = substr(line, index(line, "=") + 1); seen[k] = 1
}
k == "PROJECT_ENVIRONMENT"  { print k "=test"; next }
k == "PROJECT_NAME"         { print k "=\"" strip(v) "_test\""; next }
k == "PROJECT_DATA_DIR"     { print k "=\"" tdir "\""; next }
k == "POSTGRES_DB"          { print k "=\"" pgdb "_test\""; next }
k == "REDIS_DB"             { print k "=15"; next }  # isolated test cache, never the dev one
k in dd                     { print k "=\"" tdir "/" dd[k] "\""; next }
k in pd                     { print k "=" port(k, v); next }
k == "MASTER_BOT_TOKEN"     { print k "=123456789:TEST-mock-master-bot-token"; next }
k == "ENCRYPTION_KEY"       { print k "=" key; next }
k == "ENCRYPTION_KEYS_OLD"  { print k "="; next }
k == "ALERT_TELEGRAM_BOT_TOKEN" || k == "ALERT_TELEGRAM_CHAT_ID" { print k "="; next }  # no test alerts to the real chat
k == "APP_ENV_FILE"         { print k "=" tenv; next }
k == "GLITCHTIP_ADMIN_PASSWORD"     { print k "=" gtpw; next }  # own test obs stack values
k == "GLITCHTIP_PROJECT_PUBLIC_KEY" { print k "=" gtpk; next }
k == "SENTRY_DSN"           { print k "="; next }  # test app never reports to GlitchTip
k == "SENTRY_RELEASE"       { print k "="; next }  # computed from the app version
{ print line }
END {
    if (err) exit 2
    print ""; print "# --- added by ops/scripts/env_test.sh ---"
    for (k in dd) if (!(k in seen)) print k "=\"" tdir "/" dd[k] "\""
    for (k in pd) if (!(k in seen)) print k "=" port(k, "")
    if (err) exit 2
    if (!("ENCRYPTION_KEY" in seen)) print "ENCRYPTION_KEY=" key
    if (!("GLITCHTIP_ADMIN_PASSWORD" in seen)) print "GLITCHTIP_ADMIN_PASSWORD=" gtpw
    if (!("GLITCHTIP_PROJECT_PUBLIC_KEY" in seen)) print "GLITCHTIP_PROJECT_PUBLIC_KEY=" gtpk
    if (!("SENTRY_DSN" in seen)) print "SENTRY_DSN="
    if (!("SENTRY_RELEASE" in seen)) print "SENTRY_RELEASE="
    if (!("APP_ENV_FILE" in seen)) { print "# Env file passed to app containers (docker-compose env_file)"; print "APP_ENV_FILE=" tenv }
}
' "$src" "$src" >"$tmp"

mv "$tmp" "$dst"
trap - EXIT
echo "$key_msg"
echo "Generated $dst from $src (host ports +$port_offset)"
