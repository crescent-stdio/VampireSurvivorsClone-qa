#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_PLAYER=${1:-$QA_DEFAULT_PLAYER}
qa_require_uv
qa_require_executable "$QA_PLAYER"
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
  exec "$QA_UV_BIN" run --locked --extra trainer mlagents-learn config/qa-ppo.yaml --run-id=qa-ppo --resume --inference --seed=1234 \
    --results-dir="$QA_PROJECT_ROOT/QAArtifacts/checkpoints"
) >"$QA_PROJECT_ROOT/QAArtifacts/logs/evaluate-trainer.log" 2>&1 &
QA_TRAINER_PID=$!
"$QA_PLAYER" -batchmode -nographics -qaSeed=1234 -qaMode=evaluate \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/evaluate.log" &
QA_PLAYER_PID=$!
QA_PLAYER_STATUS=0
wait "$QA_PLAYER_PID" || QA_PLAYER_STATUS=$?
QA_PLAYER_PID=
qa_terminate_process_tree "$QA_TRAINER_PID"
QA_TRAINER_PID=
exit "$QA_PLAYER_STATUS"
