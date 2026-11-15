#!/bin/sh
# Clears the secrets of the other host in a shared env file (two-host deploy, see README.md).
# Usage: ops/scripts/env_role.sh app|obs [env file]   (make env-role ROLE=app|obs [ENV_FILE=.env])
#        ops/scripts/env_role.sh --list app|obs       (prints the secret keys of a role)
#
# One .env is created on VPS 2 (make env), filled for both hosts, completed by make obs-up
# (SENTRY_DSN, URGENTRY_BOOTSTRAP_*) and copied to VPS 1. Then:
#   ROLE=app (VPS 1, application): clears the observability secrets (OBS_SECRETS);
#   ROLE=obs (VPS 2, monitoring):  clears the application secrets (APP_SECRETS).
# SENTRY_DSN and all non-secret values are kept. Values are cleared, keys stay (`KEY=`), so
# env_check.sh finds every key of .env.example; comments, order and other lines are unchanged.
# Every `[export ]KEY=...` line of a listed key is cleared (duplicates, quoted values, `KEY= value`);
# such a line is written as `[export ]KEY=` without a trailing CR (other CRLF lines stay as is).
# Before a change the file is backed up to <file>.bak.<YYYYmmddHHMMSS> (mode 600) and replaced
# atomically (temp file in the same dir + mv, mode 600). The same clearing is applied to <file>.bak
# (backup of obs_bootstrap.sh made by make obs-up), without a further backup.
# <file>.bak.<ts> keeps the secrets (rollback of a wrong role): the script prints a warning to
# remove them with shred -u after checking. Symlinked env files are refused (mv would replace
# the link). Idempotent: nothing to clear -> files untouched. Secret values are never printed.
set -eu

APP_SECRETS="POSTGRES_PASSWORD POSTGRES_MONITORING_PASSWORD REDIS_PASSWORD RABBITMQ_PASSWORD
MINIO_SECRET_KEY STORAGE_ACCESS_KEY STORAGE_SECRET_KEY ENCRYPTION_KEY ENCRYPTION_KEYS_OLD
MASTER_BOT_TOKEN MASTER_BOT_PROXY_URL"
OBS_SECRETS="GRAFANA_ADMIN_PASSWORD CROWDSEC_BOUNCER_KEY OBS_AUTH_HASH URGENTRY_BOOTSTRAP_PASSWORD
URGENTRY_BOOTSTRAP_PAT ALERT_TELEGRAM_BOT_TOKEN"

usage() {
    echo "usage: make env-role ROLE=app|obs [ENV_FILE=.env]" >&2
    echo "  ROLE=app — VPS 1 (application): clears observability secrets" >&2
    echo "  ROLE=obs — VPS 2 (monitoring): clears application secrets" >&2
    exit 2
}

if [ "${1:-}" = --list ]; then
    case ${2:-} in
        app) printf '%s\n' "$APP_SECRETS" | tr ' ' '\n' ;;  # one key per line
        obs) printf '%s\n' "$OBS_SECRETS" | tr ' ' '\n' ;;
        *) usage ;;
    esac
    exit 0
fi

role=${1:-}
file=${2:-.env}
case $role in
    app) clear=$OBS_SECRETS; other=obs ;;
    obs) clear=$APP_SECRETS; other=app ;;
    *) usage ;;
esac
for f in "$file" "$file.bak"; do
    if [ -L "$f" ]; then
        echo "ERROR: $f is a symlink; run env-role on the real file (ENV_FILE=<target>)" >&2
        exit 1
    fi
done
[ -f "$file" ] || { echo "ERROR: $file not found" >&2; exit 1; }

tmp=""
cleanup() { [ -z "$tmp" ] || rm -f -- "$tmp"; }
trap cleanup EXIT

# clean_to SRC: writes SRC with the values of $clear removed into a new temp file $tmp.
clean_to() {
    tmp=$(mktemp "$(dirname -- "$1")/.$(basename -- "$1").role.XXXXXX")
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
    }' "$1" >"$tmp"
}

# replace SRC: atomically moves $tmp over SRC (mode 600).
replace() {
    mv -f -- "$tmp" "$1"
    tmp=""
    chmod 600 "$1"
}

clean_to "$file"
if cmp -s "$file" "$tmp"; then
    rm -f -- "$tmp"; tmp=""
    echo "OK: $file has no $other secrets (ROLE=$role), unchanged"
else
    bak="$file.bak.$(date +%Y%m%d%H%M%S)"
    [ -e "$bak" ] && bak="$bak.$$"
    (umask 077 && cp -- "$file" "$bak")
    chmod 600 "$bak"
    replace "$file"
    echo "Backup: $bak (mode 600)"
    echo "Cleared $other secrets in $file (ROLE=$role)"
fi

if [ -f "$file.bak" ]; then
    clean_to "$file.bak"
    if cmp -s "$file.bak" "$tmp"; then
        rm -f -- "$tmp"; tmp=""
    else
        replace "$file.bak"
        echo "Cleared $other secrets in $file.bak (backup of obs_bootstrap.sh)"
    fi
fi

for b in "$file".bak.[0-9]*; do
    [ -f "$b" ] || continue
    echo "WARNING: $b may contain secrets of the other host; after checking remove it: shred -u $b" >&2
done
