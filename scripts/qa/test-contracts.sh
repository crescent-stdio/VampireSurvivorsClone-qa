#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_TEST_ROOT="$QA_PROJECT_ROOT/QAArtifacts/task-6-script-contract"
QA_TEST_BIN="$QA_TEST_ROOT/bin"
mkdir -p "$QA_TEST_BIN"

printf '%s\n' '#!/bin/sh' 'printf "%s\\n" "2020.3.0f1"' >"$QA_TEST_BIN/wrong-unity"
printf '%s\n' '#!/bin/sh' 'printf "%s\\n" "6000.0.80f1"' >"$QA_TEST_BIN/right-unity"
printf '%s\n' '#!/bin/sh' 'exit 1' >"$QA_TEST_BIN/stale-uv"
chmod +x "$QA_TEST_BIN/wrong-unity" "$QA_TEST_BIN/right-unity" "$QA_TEST_BIN/stale-uv"

if UNITY_EDITOR="$QA_TEST_BIN/missing-unity" "$QA_PROJECT_ROOT/scripts/qa/build-addressables.sh" >"$QA_TEST_ROOT/missing-unity.out" 2>&1; then
  qa_fail "missing Unity contract unexpectedly succeeded"
fi
grep -F "Unity 6000.0.80f1 was not found" "$QA_TEST_ROOT/missing-unity.out" >/dev/null || qa_fail "missing Unity message was not actionable"

if UNITY_EDITOR="$QA_TEST_BIN/wrong-unity" "$QA_PROJECT_ROOT/scripts/qa/build-addressables.sh" >"$QA_TEST_ROOT/wrong-unity.out" 2>&1; then
  qa_fail "wrong Unity contract unexpectedly succeeded"
fi
grep -F "Unity 6000.0.80f1 is required" "$QA_TEST_ROOT/wrong-unity.out" >/dev/null || qa_fail "wrong Unity message was not actionable"

if UNITY_EDITOR="$QA_TEST_BIN/right-unity" UV_BIN="$QA_TEST_BIN/missing-uv" "$QA_PROJECT_ROOT/scripts/qa/setup.sh" >"$QA_TEST_ROOT/missing-uv.out" 2>&1; then
  qa_fail "missing uv contract unexpectedly succeeded"
fi
grep -F "uv was not found" "$QA_TEST_ROOT/missing-uv.out" >/dev/null || qa_fail "missing uv message was not actionable"
grep -F "qa config:" "$QA_TEST_ROOT/missing-uv.out" >/dev/null || qa_fail "missing uv must be classified as a configuration failure"

if UNITY_EDITOR="$QA_TEST_BIN/right-unity" UV_BIN="$QA_TEST_BIN/stale-uv" "$QA_PROJECT_ROOT/scripts/qa/setup.sh" >"$QA_TEST_ROOT/stale-lock.out" 2>&1; then
  qa_fail "stale uv lock contract unexpectedly succeeded"
fi
grep -F "uv.lock is missing or out of date" "$QA_TEST_ROOT/stale-lock.out" >/dev/null || qa_fail "stale lock message was not actionable"

printf '%s\n' \
  '#!/bin/sh' \
  'if [ "${1:-}" = lock ]; then exit 0; fi' \
  '[ "${1:-}" = run ] && [ "${2:-}" = --locked ] || exit 64' \
  'shift 2' \
  '[ "${1:-}" = python ] || exit 64' \
  'shift' \
  'exec "$QA_PROJECT_ROOT/.venv/bin/python" "$@"' >"$QA_TEST_BIN/uv-run"
chmod +x "$QA_TEST_BIN/uv-run"
export QA_PROJECT_ROOT
UV_BIN="$QA_TEST_BIN/uv-run" "$QA_PROJECT_ROOT/scripts/qa/run-llm-agent.sh" --help >"$QA_TEST_ROOT/llm-help.out" 2>&1 || qa_fail "LLM runner help must work without an API key or player build"
grep -F -- "--seed" "$QA_TEST_ROOT/llm-help.out" >/dev/null || qa_fail "LLM runner help must document the required seed"

grep -F -- "--burst-disable-compilation" "$QA_PROJECT_ROOT/scripts/qa/build-player.sh" >/dev/null || qa_fail "player build must use the Burst 1.6.6 macOS compatibility option"
grep -F 'm_EditorVersion: 6000.0.80f1' "$QA_PROJECT_ROOT/ProjectSettings/ProjectVersion.txt" >/dev/null || qa_fail "project must target Unity 6000.0.80f1"
grep -F 'com.unity.ml-agents#release_23' "$QA_PROJECT_ROOT/Packages/manifest.json" >/dev/null || qa_fail "project must use ML-Agents Release 23"
grep -A2 '"com.unity.ai.inference"' "$QA_PROJECT_ROOT/Packages/packages-lock.json" | grep -F '"version": "2.6.1"' >/dev/null || qa_fail "Unity 6000.0.80f1 must pin its minimum supported Inference Engine 2.6.1"
grep -F -- '--burst-disable-compilation' "$QA_PROJECT_ROOT/scripts/qa/test-editmode.sh" >/dev/null || qa_fail "EditMode tests must disable Burst AOT compilation"
grep -F -- '--burst-disable-compilation' "$QA_PROJECT_ROOT/scripts/qa/test-playmode.sh" >/dev/null || qa_fail "PlayMode tests must disable Burst AOT compilation"
grep -F "project_mgd_vampire" "$QA_PROJECT_ROOT/scripts/qa/common.sh" >/dev/null || qa_fail "default player executable must match the built macOS product"
grep -F 'FailureReason' "$QA_PROJECT_ROOT/scripts/qa/smoke.sh" >/dev/null || qa_fail "smoke failures must require a classification"
if grep -F -- '-qaSeed=1234' "$QA_PROJECT_ROOT/scripts/qa/replay.sh" >/dev/null; then
  qa_fail "replay must bootstrap from the trace seed instead of a hardcoded seed"
fi

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

if QA_SMOKE_RUNTIME_ROOT="$QA_STUB_RUNTIME_ROOT/unsupported-scale" \
  QA_SMOKE_SEEDS="9301" \
  QA_SMOKE_TIME_SCALE=20 \
  "$QA_PROJECT_ROOT/scripts/qa/smoke.sh" "$QA_SMOKE_CONTRACT_BIN/crash-player" >"$QA_SMOKE_CONTRACT_ROOT/unsupported-scale.out" 2>&1; then
  qa_fail "unsupported smoke acceleration must fail before player launch"
fi
grep -F "QA_SMOKE_TIME_SCALE must be between 1 and 4" "$QA_SMOKE_CONTRACT_ROOT/unsupported-scale.out" >/dev/null || qa_fail "unsupported smoke acceleration must be actionable"

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

QA_TRAIN_CONTRACT_ROOT="$QA_PROJECT_ROOT/QAArtifacts/train-config-contract"
QA_TRAIN_CONTRACT_BIN="$QA_TRAIN_CONTRACT_ROOT/bin"
mkdir -p "$QA_TRAIN_CONTRACT_BIN"
printf '%s\n' \
  '#!/bin/sh' \
  'if [ "${1:-}" = lock ]; then exit 0; fi' \
  'if [ "${5:-}" = python ]; then' \
  '  [ "${QA_TEST_MPS_AVAILABLE:-0}" = 1 ]' \
  '  exit' \
  'fi' \
  'printf "%s\n" "$@" >"$QA_TRAIN_CONTRACT_ROOT/arguments"' \
  'exit 0' >"$QA_TRAIN_CONTRACT_BIN/uv"
mkdir -p "$QA_TRAIN_CONTRACT_ROOT/QaGameplay.app/Contents/MacOS"
printf '%s\n' '#!/bin/sh' 'exit 0' >"$QA_TRAIN_CONTRACT_ROOT/QaGameplay.app/Contents/MacOS/project_mgd_vampire"
chmod +x "$QA_TRAIN_CONTRACT_BIN/uv" "$QA_TRAIN_CONTRACT_ROOT/QaGameplay.app/Contents/MacOS/project_mgd_vampire"
export QA_TRAIN_CONTRACT_ROOT
printf '%s\n' 'behaviors: {}' >"$QA_TRAIN_CONTRACT_ROOT/override.yaml"

UV_BIN="$QA_TRAIN_CONTRACT_BIN/uv" \
  "$QA_PROJECT_ROOT/scripts/qa/train.sh" "$QA_TRAIN_CONTRACT_ROOT/QaGameplay.app"
grep -Fx -- '--torch-device=cpu' "$QA_TRAIN_CONTRACT_ROOT/arguments" >/dev/null || qa_fail "training must default to the CPU torch device"
grep -Fx -- "--env=$QA_TRAIN_CONTRACT_ROOT/QaGameplay.app" "$QA_TRAIN_CONTRACT_ROOT/arguments" >/dev/null || qa_fail "ML-Agents training must receive the macOS app bundle"
grep -Fx -- '-qaPreset=train' "$QA_TRAIN_CONTRACT_ROOT/arguments" >/dev/null || qa_fail "training must select the train preset so the agent character can die"

QA_TORCH_DEVICE=mps QA_TEST_MPS_AVAILABLE=1 \
  QA_PPO_CONFIG="$QA_TRAIN_CONTRACT_ROOT/override.yaml" \
  QA_PPO_RUN_ID=qa-contract \
  QA_PPO_RESULTS_DIR=QAArtifacts/contract-checkpoints \
  UV_BIN="$QA_TRAIN_CONTRACT_BIN/uv" \
  "$QA_PROJECT_ROOT/scripts/qa/train.sh" "$QA_TRAIN_CONTRACT_ROOT/QaGameplay.app"
grep -Fx -- '--torch-device=mps' "$QA_TRAIN_CONTRACT_ROOT/arguments" >/dev/null || qa_fail "training must forward an available MPS torch device"
grep -Fx -- "$QA_TRAIN_CONTRACT_ROOT/override.yaml" "$QA_TRAIN_CONTRACT_ROOT/arguments" >/dev/null || qa_fail "training must allow a QA_PPO_CONFIG override"
grep -Fx -- '--run-id=qa-contract' "$QA_TRAIN_CONTRACT_ROOT/arguments" >/dev/null || qa_fail "training must allow a QA_PPO_RUN_ID override"
grep -Fx -- '--results-dir=QAArtifacts/contract-checkpoints' "$QA_TRAIN_CONTRACT_ROOT/arguments" >/dev/null || qa_fail "training must allow a QA_PPO_RESULTS_DIR override"

if QA_TORCH_DEVICE=cuda UV_BIN="$QA_TRAIN_CONTRACT_BIN/uv" \
  "$QA_PROJECT_ROOT/scripts/qa/train.sh" "$QA_TRAIN_CONTRACT_ROOT/QaGameplay.app" >"$QA_TRAIN_CONTRACT_ROOT/invalid-device.out" 2>&1; then
  qa_fail "an unsupported torch device must fail before training"
fi
grep -F 'QA_TORCH_DEVICE must be cpu or mps' "$QA_TRAIN_CONTRACT_ROOT/invalid-device.out" >/dev/null || qa_fail "unsupported torch device failure must be actionable"

if QA_TORCH_DEVICE=mps QA_TEST_MPS_AVAILABLE=0 UV_BIN="$QA_TRAIN_CONTRACT_BIN/uv" \
  "$QA_PROJECT_ROOT/scripts/qa/train.sh" "$QA_TRAIN_CONTRACT_ROOT/QaGameplay.app" >"$QA_TRAIN_CONTRACT_ROOT/unavailable-mps.out" 2>&1; then
  qa_fail "unavailable MPS must fail before training"
fi
grep -F 'MPS is not available' "$QA_TRAIN_CONTRACT_ROOT/unavailable-mps.out" >/dev/null || qa_fail "unavailable MPS failure must be actionable"

QA_EVALUATE_CONTRACT_ROOT="$QA_PROJECT_ROOT/QAArtifacts/evaluate-process-contract"
QA_EVALUATE_CONTRACT_BIN="$QA_EVALUATE_CONTRACT_ROOT/bin"
mkdir -p "$QA_EVALUATE_CONTRACT_BIN"
rm -f "$QA_EVALUATE_CONTRACT_ROOT/uv.pid" "$QA_EVALUATE_CONTRACT_ROOT/trainer.pid" \
  "$QA_EVALUATE_CONTRACT_ROOT/trainer-arguments" "$QA_EVALUATE_CONTRACT_ROOT/player-arguments"
printf '%s\n' \
  '#!/bin/sh' \
  'if [ "${1:-}" = lock ]; then exit 0; fi' \
  'printf "%s\n" "$@" >"$QA_EVALUATE_CONTRACT_ROOT/trainer-arguments"' \
  'printf "%s\n" "$$" >"$QA_EVALUATE_CONTRACT_ROOT/uv.pid"' \
  'sh -c '\''trap "exit 0" TERM INT; while :; do sleep 1; done'\'' &' \
  'child=$!' \
  'cleanup() { kill -TERM "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; exit 0; }' \
  'trap cleanup TERM INT' \
  'printf "%s\n" "$child" >"$QA_EVALUATE_CONTRACT_ROOT/trainer.pid"' \
  'wait "$child"' >"$QA_EVALUATE_CONTRACT_BIN/uv"
mkdir -p "$QA_EVALUATE_CONTRACT_ROOT/QaGameplay.app/Contents/MacOS"
printf '%s\n' \
  '#!/bin/sh' \
  'printf "%s\n" "$@" >"$QA_EVALUATE_CONTRACT_ROOT/player-arguments"' \
  'while :; do sleep 1; done' >"$QA_EVALUATE_CONTRACT_ROOT/QaGameplay.app/Contents/MacOS/project_mgd_vampire"
chmod +x "$QA_EVALUATE_CONTRACT_BIN/uv" "$QA_EVALUATE_CONTRACT_ROOT/QaGameplay.app/Contents/MacOS/project_mgd_vampire"
export QA_EVALUATE_CONTRACT_ROOT
UV_BIN="$QA_EVALUATE_CONTRACT_BIN/uv" "$QA_PROJECT_ROOT/scripts/qa/evaluate.sh" "$QA_EVALUATE_CONTRACT_ROOT/QaGameplay.app" >"$QA_EVALUATE_CONTRACT_ROOT/evaluate.out" 2>&1 &
QA_EVALUATE_PID=$!
QA_EVALUATE_READY=0
for _ in 1 2 3 4 5; do
  if [ -f "$QA_EVALUATE_CONTRACT_ROOT/uv.pid" ] && [ -f "$QA_EVALUATE_CONTRACT_ROOT/trainer.pid" ]; then
    QA_EVALUATE_READY=1
    break
  fi
  sleep 1
done
[ "$QA_EVALUATE_READY" = 1 ] || qa_fail "evaluate process contract did not start the uv trainer"
grep -Fx -- '--torch-device=cpu' "$QA_EVALUATE_CONTRACT_ROOT/trainer-arguments" >/dev/null || qa_fail "evaluation must default to the CPU torch device"
grep -Fx -- '--seed=1234' "$QA_EVALUATE_CONTRACT_ROOT/trainer-arguments" >/dev/null || qa_fail "evaluation must pass its seed to the trainer"
grep -Fx -- '-qaSeed=1234' "$QA_EVALUATE_CONTRACT_ROOT/player-arguments" >/dev/null || qa_fail "evaluation must pass the same seed to Unity"
grep -Fx -- '-qaMode=evaluate' "$QA_EVALUATE_CONTRACT_ROOT/player-arguments" >/dev/null || qa_fail "evaluation must request Unity single-episode mode"
grep -Fx -- '-qaTimeScale=1' "$QA_EVALUATE_CONTRACT_ROOT/player-arguments" >/dev/null || qa_fail "evaluation must run at normal game speed"
grep -Fx -- '-qaPreset=eval' "$QA_EVALUATE_CONTRACT_ROOT/player-arguments" >/dev/null || qa_fail "evaluation must select the eval preset so it matches the training environment"
kill -TERM "$QA_EVALUATE_PID"
wait "$QA_EVALUATE_PID" 2>/dev/null || true
QA_EVALUATE_UV_PID=$(sed -n '1p' "$QA_EVALUATE_CONTRACT_ROOT/uv.pid")
QA_EVALUATE_TRAINER_PID=$(sed -n '1p' "$QA_EVALUATE_CONTRACT_ROOT/trainer.pid")
for _ in 1 2 3 4 5; do
  if ! kill -0 "$QA_EVALUATE_UV_PID" 2>/dev/null && ! kill -0 "$QA_EVALUATE_TRAINER_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done
if kill -0 "$QA_EVALUATE_UV_PID" 2>/dev/null || kill -0 "$QA_EVALUATE_TRAINER_PID" 2>/dev/null; then
  qa_fail "evaluate cleanup left a uv or trainer process alive"
fi

if QA_EVALUATE_SEED=invalid UV_BIN="$QA_EVALUATE_CONTRACT_BIN/uv" \
  "$QA_PROJECT_ROOT/scripts/qa/evaluate.sh" "$QA_EVALUATE_CONTRACT_ROOT/QaGameplay.app" >"$QA_EVALUATE_CONTRACT_ROOT/invalid-seed.out" 2>&1; then
  qa_fail "an invalid evaluation seed must fail before launch"
fi
grep -F 'QA_EVALUATE_SEED must be a positive integer' "$QA_EVALUATE_CONTRACT_ROOT/invalid-seed.out" >/dev/null || qa_fail "invalid evaluation seed failure must be actionable"

printf '%s\n' "QA shell contracts passed."
