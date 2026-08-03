#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_PLAYER=${1:-$QA_DEFAULT_PLAYER}
QA_SMOKE_SEEDS=${QA_SMOKE_SEEDS:-"8201 8202 8203 8204 8205 8206 8207 8208 8209 8210"}
QA_SMOKE_TIME_SCALE=${QA_SMOKE_TIME_SCALE:-10}
QA_SMOKE_WALL_TIMEOUT_SECONDS=${QA_SMOKE_WALL_TIMEOUT_SECONDS:-60}
QA_SMOKE_TERMINATION_GRACE_SECONDS=${QA_SMOKE_TERMINATION_GRACE_SECONDS:-5}
QA_SMOKE_RUNTIME_ROOT=${QA_SMOKE_RUNTIME_ROOT:-"$QA_PROJECT_ROOT/QAArtifacts/player/QAArtifacts"}
qa_require_executable "$QA_PLAYER"
mkdir -p "$QA_PROJECT_ROOT/QAArtifacts/logs" "$QA_PROJECT_ROOT/QAArtifacts/screenshots"

case "$QA_SMOKE_WALL_TIMEOUT_SECONDS" in
  ''|*[!0-9]*) qa_fail "QA_SMOKE_WALL_TIMEOUT_SECONDS must be a positive integer." ;;
esac
case "$QA_SMOKE_TERMINATION_GRACE_SECONDS" in
  ''|*[!0-9]*) qa_fail "QA_SMOKE_TERMINATION_GRACE_SECONDS must be a positive integer." ;;
esac
[ "$QA_SMOKE_WALL_TIMEOUT_SECONDS" -gt 0 ] || qa_fail "QA_SMOKE_WALL_TIMEOUT_SECONDS must be a positive integer."
[ "$QA_SMOKE_TERMINATION_GRACE_SECONDS" -gt 0 ] || qa_fail "QA_SMOKE_TERMINATION_GRACE_SECONDS must be a positive integer."

QA_SMOKE_TOTAL=0
QA_SMOKE_FAILURES=0
QA_SMOKE_INFRA_FAILURES=0
QA_SMOKE_FINAL_BOSS_REACHES=0

for QA_SMOKE_SEED in $QA_SMOKE_SEEDS; do
  QA_SMOKE_PADDED_SEED=$(printf '%08d' "$QA_SMOKE_SEED")
  set -- "$QA_SMOKE_RUNTIME_ROOT"/episode-"$QA_SMOKE_PADDED_SEED"-*/summary.json
  if [ -f "$1" ]; then
    qa_fail "Smoke seed $QA_SMOKE_SEED already has an episode artifact. Choose unused QA_SMOKE_SEEDS."
  fi

  QA_SMOKE_LOG="$QA_PROJECT_ROOT/QAArtifacts/logs/smoke-seed-$QA_SMOKE_PADDED_SEED.log"
  QA_SMOKE_WATCHDOG_MARKER="$QA_PROJECT_ROOT/QAArtifacts/logs/smoke-seed-$QA_SMOKE_PADDED_SEED.watchdog"
  rm -f "$QA_SMOKE_WATCHDOG_MARKER"
  "$QA_PLAYER" -batchmode -qaMode=smoke -qaSeed="$QA_SMOKE_SEED" -qaTimeScale="$QA_SMOKE_TIME_SCALE" -logFile "$QA_SMOKE_LOG" &
  QA_SMOKE_PLAYER_PID=$!
  (
    QA_WATCHDOG_ELAPSED=0
    while kill -0 "$QA_SMOKE_PLAYER_PID" 2>/dev/null; do
      if [ "$QA_WATCHDOG_ELAPSED" -ge "$QA_SMOKE_WALL_TIMEOUT_SECONDS" ]; then
        printf '%s\n' "WallClockTimeout" >"$QA_SMOKE_WATCHDOG_MARKER"
        kill -TERM "$QA_SMOKE_PLAYER_PID" 2>/dev/null || true
        QA_WATCHDOG_GRACE_ELAPSED=0
        while kill -0 "$QA_SMOKE_PLAYER_PID" 2>/dev/null &&
          [ "$QA_WATCHDOG_GRACE_ELAPSED" -lt "$QA_SMOKE_TERMINATION_GRACE_SECONDS" ]; do
          sleep 1
          QA_WATCHDOG_GRACE_ELAPSED=$((QA_WATCHDOG_GRACE_ELAPSED + 1))
        done
        if kill -0 "$QA_SMOKE_PLAYER_PID" 2>/dev/null; then
          kill -KILL "$QA_SMOKE_PLAYER_PID" 2>/dev/null || true
        fi
        exit 0
      fi
      sleep 1
      QA_WATCHDOG_ELAPSED=$((QA_WATCHDOG_ELAPSED + 1))
    done
  ) &
  QA_SMOKE_WATCHDOG_PID=$!

  set +e
  wait "$QA_SMOKE_PLAYER_PID"
  QA_SMOKE_EXIT=$?
  set -e
  kill "$QA_SMOKE_WATCHDOG_PID" 2>/dev/null || true
  wait "$QA_SMOKE_WATCHDOG_PID" 2>/dev/null || true
  QA_SMOKE_TOTAL=$((QA_SMOKE_TOTAL + 1))
  QA_SMOKE_WATCHDOG_TRIGGERED=0

  if [ -f "$QA_SMOKE_WATCHDOG_MARKER" ]; then
    QA_SMOKE_WATCHDOG_TRIGGERED=1
    QA_SMOKE_INFRA_FAILURES=$((QA_SMOKE_INFRA_FAILURES + 1))
    QA_SMOKE_FAILURES=$((QA_SMOKE_FAILURES + 1))
    QA_SMOKE_WATCHDOG_MESSAGE="qa: seed $QA_SMOKE_SEED classified as WallClockTimeout; see $QA_SMOKE_LOG"
    printf '%s\n' "$QA_SMOKE_WATCHDOG_MESSAGE" >>"$QA_SMOKE_LOG"
    printf '%s\n' "$QA_SMOKE_WATCHDOG_MESSAGE" >&2
  fi

  set -- "$QA_SMOKE_RUNTIME_ROOT"/episode-"$QA_SMOKE_PADDED_SEED"-*/summary.json
  if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    printf '%s\n' "qa: seed $QA_SMOKE_SEED produced no unique summary or complete failure artifacts; see $QA_SMOKE_LOG" >&2
    if [ "$QA_SMOKE_WATCHDOG_TRIGGERED" -eq 0 ]; then
      QA_SMOKE_INFRA_FAILURES=$((QA_SMOKE_INFRA_FAILURES + 1))
      QA_SMOKE_FAILURES=$((QA_SMOKE_FAILURES + 1))
    fi
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

printf '%s\n' "Smoke episodes: $QA_SMOKE_TOTAL; failures: $QA_SMOKE_FAILURES; infrastructure failures: $QA_SMOKE_INFRA_FAILURES; final-boss reaches: $QA_SMOKE_FINAL_BOSS_REACHES"
[ "$QA_SMOKE_TOTAL" -eq 10 ] || qa_fail "Exactly ten smoke seeds are required."
[ "$QA_SMOKE_INFRA_FAILURES" -eq 0 ] || qa_fail "$QA_SMOKE_INFRA_FAILURES smoke infrastructure failure(s) violated the artifact contract."
[ "$QA_SMOKE_FINAL_BOSS_REACHES" -ge 1 ] || qa_fail "No smoke episode reached the final-boss phase."
