#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

# Distributable linux build. The generator switches the active build target, rebuilds
# Addressables for it, then builds the player, because BuildPlayerContent produces
# bundles for whichever target is active at the time.
qa_run_unity --burst-disable-compilation -quit -executeMethod Vampire.Editor.QA.QaAssetGenerator.BuildLinuxPlayerForBatchMode \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/player-build-linux.log"
