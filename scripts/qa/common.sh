#!/bin/sh

set -eu

qa_fail() {
  printf '%s\n' "qa: $*" >&2
  exit 1
}

qa_fail_config() {
  printf '%s\n' "qa config: $*" >&2
  exit 2
}

QA_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
QA_PROJECT_ROOT=$(CDPATH= cd -- "$QA_SCRIPT_DIR/../.." && pwd)
QA_UNITY_VERSION=2021.3.21f1
QA_DEFAULT_UNITY_EDITOR="/Applications/Unity/Hub/Editor/$QA_UNITY_VERSION/Unity.app/Contents/MacOS/Unity"
QA_DEFAULT_PLAYER="$QA_PROJECT_ROOT/QAArtifacts/player/QaGameplay.app/Contents/MacOS/project_mgd_vampire"

qa_require_unity() {
  QA_UNITY_BIN=${UNITY_EDITOR:-$QA_DEFAULT_UNITY_EDITOR}
  [ -x "$QA_UNITY_BIN" ] || qa_fail "Unity $QA_UNITY_VERSION was not found. Set UNITY_EDITOR to that editor executable."
  QA_UNITY_ACTUAL_VERSION=$("$QA_UNITY_BIN" -version 2>/dev/null || true)
  case "$QA_UNITY_ACTUAL_VERSION" in
    *"$QA_UNITY_VERSION"*) ;;
    *) qa_fail "Unity $QA_UNITY_VERSION is required; selected editor reports '$QA_UNITY_ACTUAL_VERSION'." ;;
  esac
}

qa_require_uv() {
  if [ -n "${UV_BIN:-}" ]; then
    QA_UV_BIN=$UV_BIN
  else
    QA_UV_BIN=$(command -v uv || true)
  fi
  [ -n "${QA_UV_BIN:-}" ] && [ -x "$QA_UV_BIN" ] || qa_fail_config "uv was not found. Install uv 0.12 and ensure it is available on PATH."
  "$QA_UV_BIN" lock --check --project "$QA_PROJECT_ROOT" >/dev/null 2>&1 || qa_fail_config "uv.lock is missing or out of date. Run 'uv lock' and commit the result."
}

qa_require_file() {
  [ -f "$1" ] || qa_fail "Required file does not exist: $1"
}

qa_require_executable() {
  [ -x "$1" ] || qa_fail "Required executable does not exist: $1"
}

qa_run_unity() {
  qa_require_unity
  mkdir -p "$QA_PROJECT_ROOT/QAArtifacts/logs"
  "$QA_UNITY_BIN" -batchmode -nographics -projectPath "$QA_PROJECT_ROOT" "$@"
}

qa_resolve_input_file() {
  case "$1" in
    /*) QA_RESOLVED_INPUT=$1 ;;
    *) QA_RESOLVED_INPUT=$QA_PROJECT_ROOT/$1 ;;
  esac
  qa_require_file "$QA_RESOLVED_INPUT"
}
