#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

"$(dirname -- "$0")/build-addressables.sh"
qa_run_unity -quit -executeMethod Vampire.Editor.QA.QaAssetGenerator.BuildMacPlayerForBatchMode \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/player-build.log"
