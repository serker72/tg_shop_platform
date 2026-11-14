#!/usr/bin/env bash
# Mode-dependent variables of the observability stack (used by the obs-* targets of the Makefile).
# Usage (after the env file is loaded into the environment):
#   eval "$(bash ops/scripts/obs_mode.sh [--check | --lenient])"
#
# Reads CADDY_MODE (ip | domain; required), VPS_IP, GRAFANA_DOMAIN, URGENTRY_DOMAIN,
# CADDY_HTTPS_PORT_HOST and prints `export ...` for:
#   CADDY_MODE, GRAFANA_ROOT_URL, GRAFANA_SERVE_FROM_SUB_PATH, URGENTRY_PUBLIC_URL
#   ip:     https://<VPS_IP>:<port>/grafana/ + true;  urgentry https://<VPS_IP>:<port>
#   domain: https://<GRAFANA_DOMAIN>:<port>/ + false; urgentry https://<URGENTRY_DOMAIN>:<port>
# --check (obs-up / obs-config): the variables of the selected mode must be set and
#   OBS_AUTH_USER / OBS_AUTH_HASH must be present (base64 of a bcrypt hash, `make obs-auth`),
#   CROWDSEC_BOUNCER_KEY must be hex (>= 32 chars; `make env` generates it).
# Without --check missing VPS_IP/domains fall back to defaults, but CADDY_MODE is still required:
# an old .env without it must not silently switch a domain server to the ip mode
# (obs-auth recreates caddy with these values).
# --lenient (obs-down/ps/logs only: nothing is (re)created) also defaults CADDY_MODE to ip.
# Invalid values are always rejected (they are interpolated into URLs).
set -euo pipefail

check=0 lenient=0
case ${1:-} in
    --check) check=1 ;;
    --lenient) lenient=1 ;;
    '') ;;
    *) echo "usage: obs_mode.sh [--check | --lenient]" >&2; exit 2 ;;
esac
die() { echo "ERROR: $*" >&2; exit 1; }

if [[ -z ${CADDY_MODE:-} ]] && (( ! lenient )); then
    die "CADDY_MODE is not set: add CADDY_MODE=domain (GRAFANA_DOMAIN/URGENTRY_DOMAIN, as before)" \
        "or CADDY_MODE=ip (VPS_IP) to the env file, then make obs-auth and make obs-up;" \
        "see README, section 'Обновление существующего .env'"
fi

is_ipv4() {
    local ip=$1 o
    [[ $ip =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    IFS=. read -r -a octets <<<"$ip"
    for o in "${octets[@]}"; do (( 10#$o <= 255 )) || return 1; done
}
is_domain() {
    [[ $1 =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]]
}

mode=${CADDY_MODE:-ip}
port=${CADDY_HTTPS_PORT_HOST:-443}
if ! [[ $port =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    die "CADDY_HTTPS_PORT_HOST=$port is not a port number"
fi

case $mode in
    ip)
        ip=${VPS_IP:-}
        if [[ -z $ip ]]; then
            if (( check )); then
                die "CADDY_MODE=ip requires VPS_IP (public IPv4 of VPS 2; locally 127.0.0.1)"
            fi
            ip=127.0.0.1
        fi
        is_ipv4 "$ip" || die "VPS_IP=$ip is not an IPv4 address (for a domain use CADDY_MODE=domain)"
        grafana_url="https://$ip:$port/grafana/"; sub_path=true
        urgentry_url="https://$ip:$port"
        ;;
    domain)
        gd=${GRAFANA_DOMAIN:-} td=${URGENTRY_DOMAIN:-}
        if [[ -z $gd || -z $td ]]; then
            if (( check )); then
                die "CADDY_MODE=domain requires GRAFANA_DOMAIN and URGENTRY_DOMAIN (e.g. grafana.<ip>.nip.io)"
            fi
            gd=${gd:-grafana.localhost} td=${td:-urgentry.localhost}
        fi
        is_domain "$gd" || die "GRAFANA_DOMAIN=$gd is not a domain name"
        is_domain "$td" || die "URGENTRY_DOMAIN=$td is not a domain name"
        grafana_url="https://$gd:$port/"; sub_path=false
        urgentry_url="https://$td:$port"
        ;;
    *) die "CADDY_MODE=$mode: expected ip or domain" ;;
esac

if (( check )); then
    [[ ${OBS_AUTH_USER:-} =~ ^[A-Za-z0-9._@-]+$ ]] \
        || die "OBS_AUTH_USER is empty or invalid (letters, digits, . _ @ -); run: make obs-auth"
    [[ -n ${OBS_AUTH_HASH:-} ]] || die "OBS_AUTH_HASH is empty; run: make obs-auth"
    decoded=$(printf '%s' "$OBS_AUTH_HASH" | base64 -d 2>/dev/null || true)
    bcrypt_re="^[$]2[aby][$]"
    [[ $decoded =~ $bcrypt_re ]] \
        || die "OBS_AUTH_HASH must be base64 of a bcrypt hash (without \$); run: make obs-auth"
    [[ ${CROWDSEC_BOUNCER_KEY:-} =~ ^[0-9a-fA-F]{32,}$ ]] \
        || die "CROWDSEC_BOUNCER_KEY is empty or not hex (>= 32 chars): generate one" \
            "(openssl rand -hex 32) and add CROWDSEC_BOUNCER_KEY=<hex> to the env file" \
            "(make env FORCE=1 only for a NEW installation: it regenerates ALL secrets)"
fi

printf 'export CADDY_MODE=%s GRAFANA_ROOT_URL=%s GRAFANA_SERVE_FROM_SUB_PATH=%s URGENTRY_PUBLIC_URL=%s\n' \
    "$mode" "$grafana_url" "$sub_path" "$urgentry_url"
