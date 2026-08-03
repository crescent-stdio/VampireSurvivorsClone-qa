#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

[ "$#" -ge 1 ] || qa_fail "Usage: scripts/qa/replay.sh <trace.json> [player-executable]"
qa_resolve_input_file "$1"
QA_PLAYER=${2:-$QA_DEFAULT_PLAYER}
qa_require_executable "$QA_PLAYER"
mkdir -p "$QA_PROJECT_ROOT/QAArtifacts/logs"
"$QA_PLAYER" -batchmode -nographics -qaMode=replay -qaReplayPath="$QA_RESOLVED_INPUT" \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/replay.log"
