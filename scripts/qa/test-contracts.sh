#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_TEST_ROOT="$QA_PROJECT_ROOT/QAArtifacts/task-6-script-contract"
QA_TEST_BIN="$QA_TEST_ROOT/bin"
mkdir -p "$QA_TEST_BIN"

printf '%s\n' '#!/bin/sh' 'printf "%s\\n" "2020.3.0f1"' >"$QA_TEST_BIN/wrong-unity"
printf '%s\n' '#!/bin/sh' 'printf "%s\\n" "2021.3.21f1"' >"$QA_TEST_BIN/right-unity"
printf '%s\n' '#!/bin/sh' 'printf "%s\\n" "3.11.0"' >"$QA_TEST_BIN/wrong-python"
chmod +x "$QA_TEST_BIN/wrong-unity" "$QA_TEST_BIN/right-unity" "$QA_TEST_BIN/wrong-python"

if UNITY_EDITOR="$QA_TEST_BIN/missing-unity" "$QA_PROJECT_ROOT/scripts/qa/build-addressables.sh" >"$QA_TEST_ROOT/missing-unity.out" 2>&1; then
  qa_fail "missing Unity contract unexpectedly succeeded"
fi
grep -F "Unity 2021.3.21f1 was not found" "$QA_TEST_ROOT/missing-unity.out" >/dev/null || qa_fail "missing Unity message was not actionable"

if UNITY_EDITOR="$QA_TEST_BIN/wrong-unity" "$QA_PROJECT_ROOT/scripts/qa/build-addressables.sh" >"$QA_TEST_ROOT/wrong-unity.out" 2>&1; then
  qa_fail "wrong Unity contract unexpectedly succeeded"
fi
grep -F "Unity 2021.3.21f1 is required" "$QA_TEST_ROOT/wrong-unity.out" >/dev/null || qa_fail "wrong Unity message was not actionable"

if UNITY_EDITOR="$QA_TEST_BIN/right-unity" PYTHON_BIN="$QA_TEST_BIN/wrong-python" "$QA_PROJECT_ROOT/scripts/qa/setup.sh" >"$QA_TEST_ROOT/wrong-python.out" 2>&1; then
  qa_fail "wrong Python contract unexpectedly succeeded"
fi
grep -F "Python 3.8.13 is required" "$QA_TEST_ROOT/wrong-python.out" >/dev/null || qa_fail "wrong Python message was not actionable"

grep -F -- "--burst-disable-compilation" "$QA_PROJECT_ROOT/scripts/qa/build-player.sh" >/dev/null || qa_fail "player build must use the Burst 1.6.6 macOS compatibility option"
grep -F "project_mgd_vampire" "$QA_PROJECT_ROOT/scripts/qa/common.sh" >/dev/null || qa_fail "default player executable must match the built macOS product"
grep -F 'FailureReason' "$QA_PROJECT_ROOT/scripts/qa/smoke.sh" >/dev/null || qa_fail "smoke failures must require a classification"

printf '%s\n' "QA shell contracts passed."
