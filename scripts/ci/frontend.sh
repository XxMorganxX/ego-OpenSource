#!/usr/bin/env bash
# CI job `frontend`: typecheck (via build) + vitest. Shared by ci.yml,
# publish-candidate's export-ci and kb-publish's verify.
set -euo pipefail
cd "$(dirname "$0")/../../KB_Frontend"

npm ci
npm run build
npm test
