#!/bin/sh
# Wait until a TCP port accepts connections (POSIX sh, no bash-isms).
#
# Usage: wait_for_it.sh host:port [-t timeout_seconds] [-q] [-- command [args...]]
#   -t N   total timeout in seconds (default 30; 0 = wait forever)
#   -q     quiet (no progress messages)
#   --     run command after the port is available (exec, exit code is preserved)
# Exit codes: 0 available, 1 timeout, 2 usage error.
# Probe: nc -z if available, otherwise bash /dev/tcp, otherwise python3.

set -eu

prog=$(basename "$0")
timeout=30
quiet=0
target=""

usage() {
    echo "Usage: $prog host:port [-t timeout] [-q] [-- command [args...]]" >&2
    exit 2
}

log() {
    if [ "$quiet" -eq 0 ]; then
        echo "$prog: $*" >&2
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        -t)
            [ $# -ge 2 ] || usage
            timeout=$2
            shift 2
            ;;
        -t*)
            timeout=${1#-t}
            shift
            ;;
        -q)
            quiet=1
            shift
            ;;
        --)
            shift
            break
            ;;
        -h | --help)
            usage
            ;;
        *:*)
            [ -z "$target" ] || usage
            target=$1
            shift
            ;;
        *)
            usage
            ;;
    esac
done

[ -n "$target" ] || usage
host=${target%:*}
port=${target##*:}
case "$timeout" in '' | *[!0-9]*) usage ;; esac
case "$port" in '' | *[!0-9]*) usage ;; esac
[ -n "$host" ] || usage

probe() {
    if command -v nc >/dev/null 2>&1; then
        nc -z -w 1 "$host" "$port" >/dev/null 2>&1
    elif command -v bash >/dev/null 2>&1; then
        # shellcheck disable=SC2016  # expanded by the inner bash, not by sh
        timeout 1 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$host" "$port" >/dev/null 2>&1
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 1).close()' \
            "$host" "$port" >/dev/null 2>&1
    else
        echo "$prog: neither nc, bash nor python3 is available" >&2
        exit 2
    fi
}

start=$(date +%s)
log "waiting for $host:$port (timeout ${timeout}s)"
while ! probe; do
    now=$(date +%s)
    if [ "$timeout" -gt 0 ] && [ $((now - start)) -ge "$timeout" ]; then
        log "timeout after ${timeout}s waiting for $host:$port"
        exit 1
    fi
    sleep 1
done
log "$host:$port is available after $(($(date +%s) - start))s"

if [ $# -gt 0 ]; then
    exec "$@"
fi
