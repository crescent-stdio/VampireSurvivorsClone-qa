#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_PLAYER=${1:-$QA_DEFAULT_PLAYER_BUNDLE}
QA_EVALUATE_SEED=${QA_EVALUATE_SEED:-1234}
QA_PRESET=${QA_PRESET:-eval}
QA_PPO_CONFIG=${QA_PPO_CONFIG:-config/qa-ppo.yaml}
QA_PPO_RUN_ID=${QA_PPO_RUN_ID:-qa-ppo}
QA_PPO_RESULTS_DIR=${QA_PPO_RESULTS_DIR:-$QA_PROJECT_ROOT/QAArtifacts/checkpoints}
qa_require_uv
qa_configure_torch_device
qa_resolve_mlagents_player "$QA_PLAYER"
case "$QA_EVALUATE_SEED" in
  ''|*[!0-9]*) qa_fail_config "QA_EVALUATE_SEED must be a positive integer." ;;
esac
[ "$QA_EVALUATE_SEED" -gt 0 ] 2>/dev/null || qa_fail_config "QA_EVALUATE_SEED must be a positive integer."
case "$QA_PPO_CONFIG" in
  /*) qa_require_file "$QA_PPO_CONFIG" ;;
  *) qa_require_file "$QA_PROJECT_ROOT/$QA_PPO_CONFIG" ;;
esac
mkdir -p "$QA_PROJECT_ROOT/QAArtifacts/logs"
QA_TRAINER_PID=
QA_PLAYER_PID=
qa_terminate_process_tree() {
  QA_TREE_ROOT=$1
  pkill -TERM -P "$QA_TREE_ROOT" 2>/dev/null || true
  kill -TERM "$QA_TREE_ROOT" 2>/dev/null || true
  wait "$QA_TREE_ROOT" 2>/dev/null || true
}
qa_cleanup_evaluation() {
  if [ -n "$QA_PLAYER_PID" ]; then
    qa_terminate_process_tree "$QA_PLAYER_PID"
    QA_PLAYER_PID=
  fi
  if [ -n "$QA_TRAINER_PID" ]; then
    qa_terminate_process_tree "$QA_TRAINER_PID"
    QA_TRAINER_PID=
  fi
}
trap qa_cleanup_evaluation 0 1 2 15
cd "$QA_PROJECT_ROOT"
(
  exec "$QA_UV_BIN" run --locked --extra trainer mlagents-learn "$QA_PPO_CONFIG" --run-id="$QA_PPO_RUN_ID" --resume --inference \
    --seed="$QA_EVALUATE_SEED" --torch-device="$QA_TORCH_DEVICE" --results-dir="$QA_PPO_RESULTS_DIR"
) >"$QA_PROJECT_ROOT/QAArtifacts/logs/evaluate-trainer.log" 2>&1 &
QA_TRAINER_PID=$!
"$QA_MLAGENTS_PLAYER_EXECUTABLE" -batchmode -nographics -qaPreset="$QA_PRESET" -qaSeed="$QA_EVALUATE_SEED" -qaMode=evaluate -qaTimeScale=1 \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/evaluate.log" &
QA_PLAYER_PID=$!
QA_PLAYER_STATUS=0
wait "$QA_PLAYER_PID" || QA_PLAYER_STATUS=$?
QA_PLAYER_PID=
qa_terminate_process_tree "$QA_TRAINER_PID"
QA_TRAINER_PID=
exit "$QA_PLAYER_STATUS"
