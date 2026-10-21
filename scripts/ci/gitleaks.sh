#!/usr/bin/env bash
# CI job `gitleaks`: secret scan of a directory (default: the repo root this
# script lives in; publish-candidate passes the export tree).
#
# Uses gitleaks from PATH when present (kb-publish's verify requires that on a
# push), warning when it is not the pinned version. Otherwise — on a CI runner — it downloads the PINNED release and
# refuses to run it unless its sha256 matches the checksum below, taken from
# the release's own gitleaks_<version>_checksums.txt. Bumping the version
# means updating both lines from that file.
set -euo pipefail
GITLEAKS_VERSION=8.21.2
GITLEAKS_SHA256=5bc41815076e6ed6ef8fbecc9d9b75bcae31f39029ceb55da08086315316e3ba

target="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"

if command -v gitleaks >/dev/null 2>&1; then
    bin=gitleaks
    # Same scanner as CI, or say so: newer rule sets flag what CI passes (and
    # older ones miss what it catches). KB_GITLEAKS_STRICT_VERSION=1 refuses.
    have="$(gitleaks version 2>/dev/null | head -n1 | tr -d '[:space:]')"
    if [ "${have#v}" != "$GITLEAKS_VERSION" ]; then
        if [ "${KB_GITLEAKS_STRICT_VERSION:-}" = "1" ]; then
            echo "scripts/ci/gitleaks.sh: REFUSED — gitleaks ${have:-unknown} on PATH, CI" \
                 "pins ${GITLEAKS_VERSION}" >&2
            exit 2
        fi
        echo "scripts/ci/gitleaks.sh: WARNING — gitleaks ${have:-unknown} on PATH, CI pins" \
             "${GITLEAKS_VERSION}; results may differ from CI" >&2
    fi
else
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) ;;
        *) echo "scripts/ci/gitleaks.sh: REFUSED — gitleaks not installed and the pinned" \
                "download is linux_x64 only" >&2
           exit 2 ;;
    esac
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    tarball="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
    curl -fsSL -o "$work/$tarball" \
        "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${tarball}"
    (cd "$work" && echo "${GITLEAKS_SHA256}  ${tarball}" | sha256sum -c -)
    tar -xzf "$work/$tarball" -C "$work" gitleaks
    bin="$work/gitleaks"
fi

"$bin" dir "$target" --no-banner --exit-code 1
