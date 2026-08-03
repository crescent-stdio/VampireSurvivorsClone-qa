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

QA_SMOKE_CONTRACT_ROOT="$QA_PROJECT_ROOT/QAArtifacts/task-7-smoke-contract"
QA_SMOKE_CONTRACT_BIN="$QA_SMOKE_CONTRACT_ROOT/bin"
QA_STUB_RUNTIME_ROOT="$QA_SMOKE_CONTRACT_ROOT/runtime-$$"
export QA_STUB_RUNTIME_ROOT
mkdir -p "$QA_SMOKE_CONTRACT_BIN" "$QA_STUB_RUNTIME_ROOT"

printf '%s\n' \
  '#!/bin/sh' \
  'seed=' \
  'log_file=' \
  'previous_argument=' \
  'for argument in "$@"; do' \
  '  if [ "$previous_argument" = -logFile ]; then log_file=$argument; previous_argument=; continue; fi' \
  '  case "$argument" in' \
  '    -qaSeed=*) seed=${argument#*=} ;;' \
  '  esac' \
  '  previous_argument=$argument' \
  'done' \
  'printf "%s\n" "stub seed $seed" >"$log_file"' \
  '[ "$seed" != 9101 ] || exit 7' \
  'padded_seed=$(printf "%08d" "$seed")' \
  'episode_dir="$QA_STUB_RUNTIME_ROOT/episode-$padded_seed-stub"' \
  'mkdir -p "$episode_dir" "$QA_STUB_RUNTIME_ROOT/screenshots"' \
  'printf "%s\n" "{\"Seed\":$seed,\"Outcome\":3,\"FailureReason\":\"SmokeDeadline\",\"Events\":[\"phase:3\"]}" >"$episode_dir/summary.json"' \
  ': >"$episode_dir/actions.jsonl"' \
  ': >"$episode_dir/telemetry.jsonl"' \
  ': >"$QA_STUB_RUNTIME_ROOT/screenshots/seed-$padded_seed.png"' \
  'exit 1' >"$QA_SMOKE_CONTRACT_BIN/crash-player"

chmod +x "$QA_SMOKE_CONTRACT_BIN/crash-player"
if QA_SMOKE_RUNTIME_ROOT="$QA_STUB_RUNTIME_ROOT" \
  QA_SMOKE_SEEDS="9101 9102 9103 9104 9105 9106 9107 9108 9109 9110" \
  "$QA_PROJECT_ROOT/scripts/qa/smoke.sh" "$QA_SMOKE_CONTRACT_BIN/crash-player" >"$QA_SMOKE_CONTRACT_ROOT/crash.out" 2>&1; then
  qa_fail "a player crash without a unique summary must fail the smoke run"
fi
grep -F "seed 9101 produced no unique summary" "$QA_SMOKE_CONTRACT_ROOT/crash.out" >/dev/null || qa_fail "crash output must identify the seed and missing summary"

if [ "${QA_TEST_SKIP_WATCHDOG:-0}" != 1 ]; then
  printf '%s\n' \
    '#!/bin/sh' \
    'log_file=' \
    'while [ "$#" -gt 0 ]; do' \
    '  if [ "$1" = -logFile ]; then shift; log_file=$1; fi' \
    '  shift' \
    'done' \
    'printf "%s\n" "hanging stub $$" >"$log_file"' \
    'printf "%s\n" "$$" >"$QA_STUB_RUNTIME_ROOT/hanging-player.pid"' \
    'trap "" TERM' \
    'while :; do sleep 1; done' >"$QA_SMOKE_CONTRACT_BIN/hanging-player"
  chmod +x "$QA_SMOKE_CONTRACT_BIN/hanging-player"

  if QA_SMOKE_RUNTIME_ROOT="$QA_STUB_RUNTIME_ROOT/watchdog" \
    QA_SMOKE_SEEDS="9201" \
    QA_SMOKE_WALL_TIMEOUT_SECONDS=1 \
    QA_SMOKE_TERMINATION_GRACE_SECONDS=1 \
    "$QA_PROJECT_ROOT/scripts/qa/smoke.sh" "$QA_SMOKE_CONTRACT_BIN/hanging-player" >"$QA_SMOKE_CONTRACT_ROOT/watchdog.out" 2>&1; then
    qa_fail "a wall-clock timeout must fail the smoke run"
  fi
  grep -F "seed 9201 classified as WallClockTimeout" "$QA_SMOKE_CONTRACT_ROOT/watchdog.out" >/dev/null || qa_fail "watchdog output must classify the seed and point to its log"
  grep -F "smoke-seed-00009201.log" "$QA_SMOKE_CONTRACT_ROOT/watchdog.out" >/dev/null || qa_fail "watchdog output must include the Unity log path"
  grep -F "WallClockTimeout" "$QA_PROJECT_ROOT/QAArtifacts/logs/smoke-seed-00009201.log" >/dev/null || qa_fail "watchdog classification must be recorded in the Unity log"
  QA_HANGING_PID=$(sed -n '1p' "$QA_STUB_RUNTIME_ROOT/hanging-player.pid")
  if kill -0 "$QA_HANGING_PID" 2>/dev/null; then
    qa_fail "watchdog left the hanging player process alive"
  fi
fi

printf '%s\n' "QA shell contracts passed."
