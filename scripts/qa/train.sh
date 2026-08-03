#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_PLAYER=${1:-$QA_DEFAULT_PLAYER}
qa_require_python
qa_require_executable "$QA_PROJECT_ROOT/.venv-qa/bin/mlagents-learn"
qa_require_executable "$QA_PLAYER"
qa_require_file "$QA_PROJECT_ROOT/config/qa-ppo.yaml"
mkdir -p "$QA_PROJECT_ROOT/QAArtifacts/logs"
cd "$QA_PROJECT_ROOT"
"$QA_PROJECT_ROOT/.venv-qa/bin/mlagents-learn" config/qa-ppo.yaml --run-id=qa-ppo --env="$QA_PLAYER" --no-graphics \
  --results-dir="$QA_PROJECT_ROOT/QAArtifacts/checkpoints" \
  >"$QA_PROJECT_ROOT/QAArtifacts/logs/train.log" 2>&1
