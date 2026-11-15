#!/bin/sh
# Clears the secrets of the other host in a shared env file (two-host deploy, see README.md).
# Usage: ops/scripts/env_role.sh app|obs [env file]   (make env-role ROLE=app|obs [ENV_FILE=.env])
#
# One .env is created on VPS 2 (make env), filled for both hosts, completed by make obs-up
# (SENTRY_DSN, URGENTRY_BOOTSTRAP_*) and copied to VPS 1. Then:
#   ROLE=app (VPS 1, application): clears the observability secrets (OBS_SECRETS);
#   ROLE=obs (VPS 2, monitoring):  clears the application secrets (APP_SECRETS).
# SENTRY_DSN and all non-secret values are kept. Values are cleared, keys stay (`KEY=`), so
# env_check.sh finds every key of .env.example; comments, order and other lines are unchanged.
# Before a change the file is backed up to <file>.bak.<YYYYmmddHHMMSS> (mode 600; plain .bak
# belongs to obs_bootstrap.sh) and replaced atomically (temp file in the same dir + mv, mode 600).
# Idempotent: nothing to clear -> no backup, file untouched. Secret values are never printed.
set -eu

APP_SECRETS="POSTGRES_PASSWORD POSTGRES_MONITORING_PASSWORD REDIS_PASSWORD RABBITMQ_PASSWORD
MINIO_SECRET_KEY STORAGE_ACCESS_KEY STORAGE_SECRET_KEY ENCRYPTION_KEY ENCRYPTION_KEYS_OLD
MASTER_BOT_TOKEN"
OBS_SECRETS="GRAFANA_ADMIN_PASSWORD CROWDSEC_BOUNCER_KEY OBS_AUTH_HASH URGENTRY_BOOTSTRAP_PASSWORD
URGENTRY_BOOTSTRAP_PAT ALERT_TELEGRAM_BOT_TOKEN"

usage() {
    echo "usage: make env-role ROLE=app|obs [ENV_FILE=.env]" >&2
    echo "  ROLE=app — VPS 1 (application): clears observability secrets" >&2
    echo "  ROLE=obs — VPS 2 (monitoring): clears application secrets" >&2
    exit 2
}

role=${1:-}
file=${2:-.env}
case $role in
    app) clear=$OBS_SECRETS; other=obs ;;
    obs) clear=$APP_SECRETS; other=app ;;
    *) usage ;;
esac
[ -f "$file" ] || { echo "ERROR: $file not found" >&2; exit 1; }

# Keys of the other role that currently have a non-empty value (names only).
cleared=""
for key in $clear; do
    if grep -Eq "^[[:space:]]*(export[[:space:]]+)?$key=[^[:space:]]" "$file"; then
        cleared="$cleared $key"
    fi
done
if [ -z "$cleared" ]; then
    echo "OK: $file has no $other secrets (ROLE=$role), unchanged"
    exit 0
fi

dir=$(dirname -- "$file")
base=$(basename -- "$file")
tmp=$(mktemp "$dir/.$base.role.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
chmod 600 "$tmp"

awk -v keys="$clear" '
BEGIN { n = split(keys, a, /[[:space:]]+/); for (i = 1; i <= n; i++) if (a[i] != "") k[a[i]] = 1 }
{
    if (match($0, /^[ \t]*(export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*=/)) {
        head = substr($0, 1, RLENGTH)
        key = head
        sub(/=$/, "", key)
        sub(/^[ \t]*(export[ \t]+)?/, "", key)
        if (key in k) { print head; next }
    }
    print
}' "$file" >"$tmp"

bak="$file.bak.$(date +%Y%m%d%H%M%S)"
[ -e "$bak" ] && bak="$bak.$$"
(umask 077 && cp -- "$file" "$bak")
chmod 600 "$bak"
mv -f -- "$tmp" "$file"
trap - EXIT
chmod 600 "$file"
echo "Backup: $bak (mode 600)"
echo "Cleared $other secrets in $file (ROLE=$role):$cleared"
