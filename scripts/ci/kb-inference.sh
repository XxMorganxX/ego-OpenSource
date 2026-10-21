#!/usr/bin/env bash
# CI job `kb-inference`: KB_Inference has no pyproject, only requirements.txt,
# so its suite runs in a throwaway uv environment built from that file. Shared
# by ci.yml, publish-candidate's export-ci and kb-publish's verify.
set -euo pipefail
cd "$(dirname "$0")/../../KB_Inference"
export UV_PYTHON="${UV_PYTHON:-3.13}"

uv run --no-project --with-requirements requirements.txt python -m pytest
