#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

qa_run_unity -quit -executeMethod Vampire.Editor.QA.QaAssetGenerator.GenerateForBatchMode \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/generate-assets.log"
