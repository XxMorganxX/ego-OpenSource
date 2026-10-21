#!/usr/bin/env bash
# CI job `python`: KB_Core, KB_Retrieval, KB_MCP and KB_Agent suites plus the
# static golden-set validation. Shared by ci.yml, publish-candidate's export-ci
# and kb-publish's verify (one definition, no drift).
#
# KB_TEST_DATABASE_URL is REQUIRED and never defaulted: the fixtures TRUNCATE
# their database, so falling back to some default would wipe whatever lives
# there. KB_TEST_DATABASE_URL_B (the second tenant for the isolation tests)
# defaults to the same URL with `_b` appended to the database name. Both are
# created if missing (template0) with the ltree + vector extensions.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ -z "${KB_TEST_DATABASE_URL:-}" ]; then
    echo "scripts/ci/python.sh: REFUSED — set KB_TEST_DATABASE_URL to a disposable" \
         "test database (the suites TRUNCATE it)" >&2
    exit 2
fi
if [ -z "${KB_TEST_DATABASE_URL_B:-}" ]; then
    base="${KB_TEST_DATABASE_URL%%\?*}"
    query="${KB_TEST_DATABASE_URL#"$base"}"
    KB_TEST_DATABASE_URL_B="${base}_b${query}"
fi
export KB_TEST_DATABASE_URL KB_TEST_DATABASE_URL_B
# Same interpreter as the live venvs (each package's .python-version says 3.13).
export UV_PYTHON="${UV_PYTHON:-3.13}"
echo "KB_TEST_DATABASE_URL_B=${KB_TEST_DATABASE_URL_B}"

(
    cd KB_Core
    uv sync --locked
    uv run --locked python ../scripts/ci/prepare_test_dbs.py
    uv run --locked pytest --cov=kb_core --cov-report=term-missing
)
for pkg in KB_Retrieval KB_MCP KB_Agent; do
    (cd "$pkg" && uv sync --locked && uv run --locked pytest)
done
(
    # Golden eval sets: structure only (--static), never a database.
    cd KB_MCP
    uv run --locked python ../evals/retrieval/validate.py ../evals/retrieval/golden.jsonl --static
    uv run --locked python ../evals/retrieval/validate.py ../evals/retrieval/golden_package.jsonl --static
    uv run --locked python ../evals/retrieval/validate.py ../evals/mcp_surface/golden.jsonl --static
)
