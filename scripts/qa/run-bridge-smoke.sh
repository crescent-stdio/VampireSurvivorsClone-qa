#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

qa_require_uv
QA_BRIDGE_PLAYER=${QA_BRIDGE_PLAYER:-$QA_PROJECT_ROOT/QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app}
QA_BRIDGE_OUTPUT=${QA_BRIDGE_OUTPUT:-$QA_PROJECT_ROOT/QAArtifacts/bridge-runs/latest}

cd "$QA_PROJECT_ROOT"
exec "$QA_UV_BIN" run --locked python -m qa_smoke.run \
  --game-exe "$QA_BRIDGE_PLAYER" \
  --project-root "$QA_PROJECT_ROOT" \
  --output "$QA_BRIDGE_OUTPUT" \
  "$@"
