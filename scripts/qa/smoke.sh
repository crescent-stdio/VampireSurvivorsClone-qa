#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_PLAYER=${1:-$QA_DEFAULT_PLAYER}
QA_SMOKE_SEEDS=${QA_SMOKE_SEEDS:-"8201 8202 8203 8204 8205 8206 8207 8208 8209 8210"}
QA_SMOKE_TIME_SCALE=${QA_SMOKE_TIME_SCALE:-10}
QA_SMOKE_RUNTIME_ROOT="$QA_PROJECT_ROOT/QAArtifacts/player/QAArtifacts"
qa_require_executable "$QA_PLAYER"
mkdir -p "$QA_PROJECT_ROOT/QAArtifacts/logs" "$QA_PROJECT_ROOT/QAArtifacts/screenshots"

QA_SMOKE_TOTAL=0
QA_SMOKE_FAILURES=0
QA_SMOKE_FINAL_BOSS_REACHES=0

for QA_SMOKE_SEED in $QA_SMOKE_SEEDS; do
  QA_SMOKE_PADDED_SEED=$(printf '%08d' "$QA_SMOKE_SEED")
  set -- "$QA_SMOKE_RUNTIME_ROOT"/episode-"$QA_SMOKE_PADDED_SEED"-*/summary.json
  if [ -f "$1" ]; then
    qa_fail "Smoke seed $QA_SMOKE_SEED already has an episode artifact. Choose unused QA_SMOKE_SEEDS."
  fi

  QA_SMOKE_LOG="$QA_PROJECT_ROOT/QAArtifacts/logs/smoke-seed-$QA_SMOKE_PADDED_SEED.log"
  set +e
  "$QA_PLAYER" -batchmode -qaMode=smoke -qaSeed="$QA_SMOKE_SEED" -qaTimeScale="$QA_SMOKE_TIME_SCALE" -logFile "$QA_SMOKE_LOG"
  QA_SMOKE_EXIT=$?
  set -e
  QA_SMOKE_TOTAL=$((QA_SMOKE_TOTAL + 1))

  set -- "$QA_SMOKE_RUNTIME_ROOT"/episode-"$QA_SMOKE_PADDED_SEED"-*/summary.json
  if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    printf '%s\n' "qa: seed $QA_SMOKE_SEED produced no unique summary; see $QA_SMOKE_LOG" >&2
    QA_SMOKE_FAILURES=$((QA_SMOKE_FAILURES + 1))
    continue
  fi

  QA_SMOKE_SUMMARY=$1
  QA_SMOKE_EPISODE_DIR=$(dirname -- "$QA_SMOKE_SUMMARY")
  qa_require_file "$QA_SMOKE_EPISODE_DIR/actions.jsonl"
  qa_require_file "$QA_SMOKE_EPISODE_DIR/telemetry.jsonl"
  qa_require_file "$QA_SMOKE_LOG"

  if grep -F '"phase:3"' "$QA_SMOKE_SUMMARY" >/dev/null; then
    QA_SMOKE_FINAL_BOSS_REACHES=$((QA_SMOKE_FINAL_BOSS_REACHES + 1))
  fi

  if [ "$QA_SMOKE_EXIT" -ne 0 ]; then
    grep -E '"FailureReason":"[^"]+"' "$QA_SMOKE_SUMMARY" >/dev/null ||
      qa_fail "Smoke seed $QA_SMOKE_SEED has an unclassified failure."
    QA_SMOKE_SCREENSHOT="$QA_SMOKE_RUNTIME_ROOT/screenshots/seed-$QA_SMOKE_PADDED_SEED.png"
    qa_require_file "$QA_SMOKE_SCREENSHOT"
    QA_SMOKE_FAILURES=$((QA_SMOKE_FAILURES + 1))
  fi
done

printf '%s\n' "Smoke episodes: $QA_SMOKE_TOTAL; failures: $QA_SMOKE_FAILURES; final-boss reaches: $QA_SMOKE_FINAL_BOSS_REACHES"
[ "$QA_SMOKE_TOTAL" -eq 10 ] || qa_fail "Exactly ten smoke seeds are required."
[ "$QA_SMOKE_FINAL_BOSS_REACHES" -ge 1 ] || qa_fail "No smoke episode reached the final-boss phase."
