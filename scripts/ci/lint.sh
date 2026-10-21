#!/usr/bin/env bash
# CI job `lint`. The one definition of this check: .github/workflows/ci.yml,
# publish-candidate's export-ci job and kb-publish's verify all run this
# script, so they cannot drift apart. Run from anywhere; it works on the repo
# root it lives in.
set -euo pipefail
cd "$(dirname "$0")/../.."

# Config in the repo-root ruff.toml.
uvx ruff check KB_Core KB_Retrieval KB_MCP KB_Inference KB_Agent evals scripts
