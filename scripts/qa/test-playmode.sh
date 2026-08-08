#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

mkdir -p "$QA_PROJECT_ROOT/QAArtifacts/TestResults"
qa_run_unity --burst-disable-compilation -runTests -testPlatform PlayMode \
  -testResults "$QA_PROJECT_ROOT/QAArtifacts/TestResults/playmode.xml" \
  -logFile "$QA_PROJECT_ROOT/QAArtifacts/logs/playmode.log"
