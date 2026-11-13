#!/usr/bin/env bash
# HTTP Basic Auth credentials of Caddy (observability stack): make obs-auth / make env.
# Usage: ops/scripts/obs_auth.sh <env file>
#
# Password source:
#   PASSWORD_STDIN=1      -> first line of stdin;
#   stdin is a terminal   -> asked interactively (hidden; empty answer = generate);
#   otherwise             -> generated (openssl rand), printed ONCE to stderr.
# The password is hashed by `caddy hash-password` (bcrypt) in a one-shot container of
# CADDY_IMAGE (env file) or the image pinned in docker-compose.obs.yml; the plaintext goes
# through stdin of the container, never through argv. The env file gets
#   OBS_AUTH_USER (kept; empty -> admin) and OBS_AUTH_HASH = base64 of the bcrypt hash
# (bcrypt hashes contain `$`, which `source`/compose would expand). The password itself is
# never stored. The env file is replaced atomically (same directory, same mode), other lines
# are kept. Exit codes: 0 ok, 1 error, 3 docker unavailable (env file untouched).
set -euo pipefail

DEFAULT_CADDY_IMAGE='caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d'

env_file=${1:?usage: obs_auth.sh <env file>}
[[ -f $env_file ]] || { echo "ERROR: $env_file not found (make env)" >&2; exit 1; }

get_env() {  # last value of KEY in env file, quotes stripped
    tr -d '\r' <"$env_file" \
        | sed -nE "s/^[[:space:]]*(export[[:space:]]+)?$1=//p" | tail -n1 \
        | sed -E 's/^[[:space:]]+|[[:space:]]+$//g' | tr -d "\"'"
}

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "WARNING: docker is unavailable, OBS_AUTH_HASH not generated; run later: make obs-auth" >&2
    exit 3
fi

image=$(get_env CADDY_IMAGE)
image=${image:-$DEFAULT_CADDY_IMAGE}
user=$(get_env OBS_AUTH_USER)
user=${user:-admin}
[[ $user =~ ^[A-Za-z0-9._@-]+$ ]] \
    || { echo "ERROR: OBS_AUTH_USER may contain only letters, digits, . _ @ -" >&2; exit 1; }

pw='' generated=0
if [[ ${PASSWORD_STDIN:-0} == 1 ]]; then
    IFS= read -r pw || true
elif [[ -t 0 ]]; then
    IFS= read -rs -p "Basic Auth password for '$user' (empty = generate): " pw; echo >&2
    if [[ -n $pw ]]; then
        IFS= read -rs -p "Repeat password: " pw2; echo >&2
        [[ $pw == "$pw2" ]] || { echo "ERROR: passwords do not match" >&2; exit 1; }
    fi
fi
if [[ -z $pw ]]; then
    pw=$(openssl rand -base64 18 | tr -d '/+=\n')
    generated=1
fi
(( ${#pw} >= 12 )) || { echo "ERROR: password must be at least 12 characters" >&2; exit 1; }

hash=$(printf '%s\n' "$pw" | docker run --rm -i "$image" caddy hash-password 2>/dev/null) \
    || { echo "ERROR: caddy hash-password failed ($image)" >&2; exit 1; }
hash=$(printf '%s' "$hash" | tr -d '\r\n')
bcrypt_re="^[$]2[aby][$]"
[[ $hash =~ $bcrypt_re ]] || { echo "ERROR: unexpected output of caddy hash-password" >&2; exit 1; }
b64=$(printf '%s' "$hash" | base64 | tr -d '\n')

tmp=$(mktemp "$(dirname -- "$env_file")/.$(basename -- "$env_file").auth.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
chmod "$(stat -c %a "$env_file")" "$tmp"
U="$user" H="$b64" awk '
    BEGIN { u = ENVIRON["U"]; h = ENVIRON["H"] }
    { line = $0; sub(/^[[:space:]]*(export[[:space:]]+)?/, "", line) }
    index(line, "OBS_AUTH_USER=") == 1 { print "OBS_AUTH_USER=" u; su = 1; next }
    index(line, "OBS_AUTH_HASH=") == 1 { print "OBS_AUTH_HASH=" h; sh = 1; next }
    { print }
    END {
        if (!su || !sh) print "\n# --- Caddy Basic Auth (make obs-auth) ---"
        if (!su) print "OBS_AUTH_USER=" u
        if (!sh) print "OBS_AUTH_HASH=" h
    }
' "$env_file" >"$tmp"
mv -f -- "$tmp" "$env_file"
trap - EXIT

echo "Updated OBS_AUTH_USER / OBS_AUTH_HASH in $env_file"
if (( generated )); then
    {
        echo
        echo "##################################################################"
        echo "#  !!! SAVE THIS PASSWORD: it is shown ONCE and NOT stored !!!"
        echo "#  Basic Auth of Caddy (Grafana/GlitchTip): user=$user password=$pw"
        if [[ ${OBS_AUTH_IMPLICIT:-0} == 1 ]]; then
            echo "#  $env_file was created implicitly (make init-dirs/obs-up/...);"
            echo "#  the password may scroll away in the output below."
        fi
        echo "#  Lost it? Set a new one: make obs-auth"
        echo "##################################################################"
        echo
    } >&2
    # if-form on purpose: a false `cond && cmd` as the last command would make the exit code 1
    if [[ ${OBS_AUTH_IMPLICIT:-0} == 1 && -t 2 ]]; then
        sleep 5  # give the operator time to copy it
    fi
fi
