#!/bin/sh
# Prints the application version: <base>.<commit number in milestone>[+dirty] (PEP 440).
#
#   base       = [project] version in pyproject.toml, form X.Y.N (N = milestone number)
#   tag        = milestone-N, annotated tag on the first commit of milestone N
#   commit no. = git rev-list --count milestone-N..HEAD + 1 (tagged commit = .1)
#   +dirty     = the working tree has uncommitted or untracked (not ignored) changes
#
# Usage: ops/scripts/version.sh [--commit | --env]
#   (no option)  version, e.g. 0.1.1.17 or 0.1.1.17+dirty
#   --commit     short hash of HEAD
#   --env        APP_VERSION=..., APP_COMMIT=..., APP_BUILD_TIME=... (UTC ISO 8601)
#
# Works from any directory inside the repository (pyproject.toml of the repo root).
# pyproject.toml is parsed with POSIX awk: no uv/python needed on the build host.
# Missing milestone tag is an error (exit 1) with a hint: an image must never be built
# with a guessed version.
set -eu

die() {
    echo "ERROR: $*" >&2
    exit 1
}

mode="version"
case "${1:-}" in
    "") ;;
    --commit) mode="commit" ;;
    --env) mode="env" ;;  # quoted: bare env is also a command name (SC2209)
    -h | --help)
        sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "ERROR: unknown option: $1 (use --commit, --env or --help)" >&2
        exit 2
        ;;
esac

command -v git >/dev/null 2>&1 || die "git not found in PATH"
root=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git repository"
commit=$(git -C "$root" rev-parse --short HEAD 2>/dev/null) || die "repository has no commits"

if [ "$mode" = commit ]; then
    echo "$commit"
    exit 0
fi

pyproject="$root/pyproject.toml"
[ -f "$pyproject" ] || die "$pyproject not found"
base=$(awk '
    /^[[:space:]]*\[/ { in_project = ($0 ~ /^[[:space:]]*\[project\][[:space:]]*$/); next }
    in_project && /^[[:space:]]*version[[:space:]]*=/ {
        sub(/^[^=]*=[[:space:]]*/, "")
        gsub(/["\047]/, "")
        sub(/[[:space:]]*(#.*)?$/, "")
        print
        exit
    }
' "$pyproject")
echo "$base" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
    || die "[project] version in $pyproject must be X.Y.N (N = milestone), got: '$base'"

milestone=${base##*.}
tag="milestone-$milestone"
git -C "$root" rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null \
    || die "tag $tag not found. Create it on the first commit of milestone $milestone:
       git tag -a $tag <sha> -m \"Milestone $milestone start\"   (see docs/ops/versioning.md)"

count=$(git -C "$root" rev-list --count "$tag..HEAD")
version="$base.$((count + 1))"
if [ -n "$(git -C "$root" status --porcelain)" ]; then
    version="$version+dirty"
fi

if [ "$mode" = env ]; then
    echo "APP_VERSION=$version"
    echo "APP_COMMIT=$commit"
    echo "APP_BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
else
    echo "$version"
fi
