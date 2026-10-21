#!/usr/bin/env bash
# CI job `evals-tests`: the pure-function tests under evals/*/tests, run in
# KB_MCP's environment (it carries ego-os-core and ego-os-retrieval). Each directory
# runs in its own pytest process: they put their module dir on sys.path and
# share the package name `tests`, so one process would mix them up.
# Shared by ci.yml, publish-candidate's export-ci and kb-publish's verify.
set -euo pipefail
cd "$(dirname "$0")/../.."
export UV_PYTHON="${UV_PYTHON:-3.13}"

shopt -s nullglob
dirs=(evals/*/tests)
if [ "${#dirs[@]}" -eq 0 ]; then
    echo "scripts/ci/evals-tests.sh: no evals/*/tests directories — nothing to run"
    exit 0
fi
uv sync --locked --project KB_MCP
for d in "${dirs[@]}"; do
    echo "== $d"
    uv run --locked --project KB_MCP python -m pytest -q -p no:cacheprovider "$d"
done
