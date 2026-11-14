#!/usr/bin/env bash
# Compares variable names of the template with local env files (values are never printed).
# Usage: ops/scripts/env_check.sh [--warn] <.env.example> <env file>...
# Only active `KEY=` lines count (commented `# KEY=` examples are optional).
# Prints missing keys per file; rc=1 if a key is missing or a file is absent
# (--warn: same report as WARNING with rc=0, used by make init-dirs).
set -euo pipefail

warn=0
if [[ ${1:-} == --warn ]]; then warn=1; shift; fi
example=${1:?usage: env_check.sh [--warn] <.env.example> <env file>...}
shift
[[ $# -gt 0 ]] || { echo "usage: env_check.sh [--warn] <.env.example> <env file>..." >&2; exit 2; }
[[ -f $example ]] || { echo "ERROR: $example not found" >&2; exit 2; }

keys() { tr -d '\r' <"$1" | sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=.*/\2/p' | sort -u; }

level=ERROR; [[ $warn == 1 ]] && level=WARNING
rc=0
for f in "$@"; do
    if [[ ! -f $f ]]; then echo "$level: $f not found" >&2; rc=1; continue; fi
    missing=$(comm -23 <(keys "$example") <(keys "$f") | tr '\n' ' ')
    if [[ -n $missing ]]; then
        echo "$level: $f lacks keys from $example: ${missing% }" >&2; rc=1
    else
        echo "OK: $f has all keys of $example"
    fi
done
[[ $rc == 0 ]] || echo "  Add them with values from $example (.env.test: make env-test)." >&2
[[ $warn == 1 ]] && exit 0
exit "$rc"
