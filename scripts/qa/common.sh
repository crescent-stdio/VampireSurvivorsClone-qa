#!/bin/sh

set -eu

qa_fail() {
  printf '%s\n' "qa: $*" >&2
  exit 1
}

QA_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
QA_PROJECT_ROOT=$(CDPATH= cd -- "$QA_SCRIPT_DIR/../.." && pwd)
QA_UNITY_VERSION=2021.3.21f1
QA_DEFAULT_UNITY_EDITOR="/Applications/Unity/Hub/Editor/$QA_UNITY_VERSION/Unity.app/Contents/MacOS/Unity"

qa_require_unity() {
  QA_UNITY_BIN=${UNITY_EDITOR:-$QA_DEFAULT_UNITY_EDITOR}
  [ -x "$QA_UNITY_BIN" ] || qa_fail "Unity $QA_UNITY_VERSION was not found. Set UNITY_EDITOR to that editor executable."
  QA_UNITY_ACTUAL_VERSION=$("$QA_UNITY_BIN" -version 2>/dev/null || true)
  case "$QA_UNITY_ACTUAL_VERSION" in
    *"$QA_UNITY_VERSION"*) ;;
    *) qa_fail "Unity $QA_UNITY_VERSION is required; selected editor reports '$QA_UNITY_ACTUAL_VERSION'." ;;
  esac
}

qa_require_python() {
  if [ -n "${PYTHON_BIN:-}" ]; then
    QA_PYTHON_BIN=$PYTHON_BIN
  else
    QA_PYTHON_BIN=$(command -v python3 || true)
  fi
  [ -n "${QA_PYTHON_BIN:-}" ] && [ -x "$QA_PYTHON_BIN" ] || qa_fail "Python 3.8.13 was not found. Set PYTHON_BIN to an exact Python 3.8.13 executable."
  QA_PYTHON_ACTUAL_VERSION=$("$QA_PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true)
  [ "$QA_PYTHON_ACTUAL_VERSION" = "3.8.13" ] || qa_fail "Python 3.8.13 is required; selected interpreter reports '$QA_PYTHON_ACTUAL_VERSION'."
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
