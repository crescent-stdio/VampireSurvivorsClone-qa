#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

# Addressables are built by the generator, inside the same session that selects the build
# target, because BuildPlayerContent produces bundles for whichever target is active.
qa_run_unity --burst-disable-compilation -quit -executeMethod Vampire.Editor.QA.QaAssetGenerator.BuildMacPlayerForBatchMode \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/player-build.log"
