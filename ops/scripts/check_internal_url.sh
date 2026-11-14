#!/usr/bin/env bash
# Checks that a URL / Sentry DSN points to an internal address (used by obs_bootstrap.sh).
# Usage: ops/scripts/check_internal_url.sh <url>   -> exit 0 internal, 1 otherwise (message on stderr)
#
# Allowed host: an IP from 10/8, 172.16/12, 192.168/16, 127/8, 100.64/10 (Tailscale CGNAT),
# ::1, fc00::/7, or a name without dots (Docker service such as urgentry, localhost).
# Same rules as app.core.config.is_internal_host (tests/unit/test_internal_url.py keeps
# both in sync). Only the host is printed, never the URL (a DSN contains the key).
set -euo pipefail

url=${1:?usage: check_internal_url.sh <url>}
python3 - "$url" <<'PY'
import ipaddress
import re
import socket
import sys
from urllib.parse import urlsplit

NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
        "100.64.0.0/10", "::1/128", "fc00::/7",
    )
)
NAME = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,61}[a-z0-9])?$")
HEX = re.compile(r"^0x[0-9a-f]*$")


def service_name(host: str) -> bool:
    # no dots, a letter, not a decimal/hex/octal inet_aton form (getaddrinfo resolves those)
    if not NAME.match(host) or not re.search(r"[a-z]", host) or HEX.match(host):
        return False
    try:
        socket.inet_aton(host)
    except OSError:
        return True
    return False


def internal(host: str) -> bool:
    host = host.strip().lower().strip("[]")
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return service_name(host)
    return any(addr in net for net in NETWORKS)


parts = urlsplit(sys.argv[1])
try:
    host = parts.hostname or ""
    _ = parts.port  # ValueError for a port outside 0..65535
except ValueError:
    host = ""
if parts.scheme not in ("http", "https") or not internal(host):
    print(
        f"ERROR: host {host!r} is not internal: use a private IP (10/8, 172.16/12, 192.168/16, "
        "127/8, 100.64/10, ::1, fc00::/7) or a Docker service name without dots "
        "(e.g. http://caddy:8081); public urgentry addresses are forbidden",
        file=sys.stderr,
    )
    sys.exit(1)
PY
