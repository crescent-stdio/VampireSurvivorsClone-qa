#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

qa_run_unity -quit -executeMethod Vampire.Editor.QA.QaAssetGenerator.BuildAddressablesForBatchMode \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/addressables.log"
