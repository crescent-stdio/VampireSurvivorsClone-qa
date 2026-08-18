#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_BRIDGE_BUILD_PATH=${QA_BRIDGE_BUILD_PATH:-$QA_PROJECT_ROOT/QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app}
export QA_BRIDGE_BUILD_PATH

qa_run_unity --burst-disable-compilation -quit \
  -executeMethod Vampire.Editor.QA.QaBridgeBuild.BuildMacPlayerForBatchMode \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/bridge-player-build-macos.log"

[ -d "$QA_BRIDGE_BUILD_PATH" ] || qa_fail "Unity returned success but did not create the Bridge player: $QA_BRIDGE_BUILD_PATH"
printf '%s\n' "$QA_BRIDGE_BUILD_PATH"
