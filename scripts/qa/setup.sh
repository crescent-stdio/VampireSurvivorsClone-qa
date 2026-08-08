#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

qa_require_unity
qa_require_uv
cd "$QA_PROJECT_ROOT"
"$QA_UV_BIN" sync --locked --extra trainer
printf '%s\n' "QA setup complete. ML-Agents 1.1.0 is Apache-2.0 licensed; review your dependency policy before distribution."
