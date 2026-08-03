#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_PLAYER=${1:-$QA_PROJECT_ROOT/QAArtifacts/player/QaGameplay.app/Contents/MacOS/QaGameplay}
qa_require_python
qa_require_executable "$QA_PROJECT_ROOT/.venv-qa/bin/mlagents-learn"
qa_require_executable "$QA_PLAYER"
mkdir -p "$QA_PROJECT_ROOT/QAArtifacts/logs"
QA_TRAINER_PID=
qa_cleanup_evaluation() {
  if [ -n "$QA_TRAINER_PID" ]; then
    kill "$QA_TRAINER_PID" 2>/dev/null || true
  fi
}
trap qa_cleanup_evaluation 0 1 2 15
cd "$QA_PROJECT_ROOT"
"$QA_PROJECT_ROOT/.venv-qa/bin/mlagents-learn" config/qa-ppo.yaml --run-id=qa-ppo --resume --inference --seed=1234 \
  --results-dir="$QA_PROJECT_ROOT/QAArtifacts/checkpoints" \
  >"$QA_PROJECT_ROOT/QAArtifacts/logs/evaluate-trainer.log" 2>&1 &
QA_TRAINER_PID=$!
"$QA_PLAYER" -batchmode -nographics -qaSeed=1234 -qaMode=evaluate \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/evaluate.log"
wait "$QA_TRAINER_PID"
QA_TRAINER_PID=
