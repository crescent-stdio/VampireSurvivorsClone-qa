#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_PLAYER=${1:-$QA_DEFAULT_PLAYER}
QA_PPO_CONFIG=${QA_PPO_CONFIG:-config/qa-ppo.yaml}
QA_PPO_RUN_ID=${QA_PPO_RUN_ID:-qa-ppo}
QA_PPO_RESULTS_DIR=${QA_PPO_RESULTS_DIR:-$QA_PROJECT_ROOT/QAArtifacts/checkpoints}
qa_require_uv
qa_configure_torch_device
qa_require_executable "$QA_PLAYER"
case "$QA_PPO_CONFIG" in
  /*) qa_require_file "$QA_PPO_CONFIG" ;;
  *) qa_require_file "$QA_PROJECT_ROOT/$QA_PPO_CONFIG" ;;
esac
mkdir -p "$QA_PROJECT_ROOT/QAArtifacts/logs"
cd "$QA_PROJECT_ROOT"
"$QA_UV_BIN" run --locked --extra trainer mlagents-learn "$QA_PPO_CONFIG" --run-id="$QA_PPO_RUN_ID" --env="$QA_PLAYER" --no-graphics \
  --torch-device="$QA_TORCH_DEVICE" --results-dir="$QA_PPO_RESULTS_DIR" \
  >"$QA_PROJECT_ROOT/QAArtifacts/logs/train.log" 2>&1
