#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

qa_require_uv
cd "$QA_PROJECT_ROOT"
exec "$QA_UV_BIN" run --locked python -m qa_llm_agent.cli "$@"
