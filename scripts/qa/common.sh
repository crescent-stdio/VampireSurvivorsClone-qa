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
QA_UNITY_VERSION=6000.0.80f1
QA_DEFAULT_UNITY_EDITOR="/Applications/Unity/Hub/Editor/$QA_UNITY_VERSION/Unity.app/Contents/MacOS/Unity"
QA_DEFAULT_PLAYER_BUNDLE="$QA_PROJECT_ROOT/QAArtifacts/player/QaGameplay.app"
QA_DEFAULT_PLAYER="$QA_DEFAULT_PLAYER_BUNDLE/Contents/MacOS/project_mgd_vampire"

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

# Read a value from config/qa-presets.json. Delegates to Python because POSIX sh has no
# JSON parser and every caller already requires uv.
# Usage: qa_preset_value <preset> <dotted-key>
qa_preset_value() {
  [ -n "${QA_UV_BIN:-}" ] || qa_fail_config "qa_preset_value requires qa_require_uv first."
  "$QA_UV_BIN" run --locked --project "$QA_PROJECT_ROOT" python -m qa_agent_runtime.presets \
    --preset "$1" --key "$2" 2>/dev/null ||
    qa_fail_config "Unable to read preset '$1' key '$2' from config/qa-presets.json."
}

qa_configure_torch_device() {
  QA_TORCH_DEVICE=${QA_TORCH_DEVICE:-cpu}
  case "$QA_TORCH_DEVICE" in
    cpu) ;;
    mps)
      "$QA_UV_BIN" run --locked --extra trainer python -c \
        'import sys, torch; sys.exit(0 if torch.backends.mps.is_available() else 1)' \
        >/dev/null 2>&1 || qa_fail_config "MPS is not available in the selected PyTorch environment. Use QA_TORCH_DEVICE=cpu."
      ;;
    *) qa_fail_config "QA_TORCH_DEVICE must be cpu or mps; received '$QA_TORCH_DEVICE'." ;;
  esac
}

qa_require_file() {
  [ -f "$1" ] || qa_fail "Required file does not exist: $1"
}

qa_require_executable() {
  [ -x "$1" ] || qa_fail "Required executable does not exist: $1"
}

qa_resolve_mlagents_player() {
  case "$1" in
    *.app)
      QA_MLAGENTS_PLAYER_BUNDLE=$1
      QA_MLAGENTS_PLAYER_EXECUTABLE=$1/Contents/MacOS/project_mgd_vampire
      ;;
    *)
      qa_require_executable "$1"
      QA_MLAGENTS_PLAYER_EXECUTABLE=$1
      QA_MLAGENTS_PLAYER_BUNDLE=$(CDPATH= cd -- "$(dirname -- "$1")/../.." && pwd)
      ;;
  esac
  [ -d "$QA_MLAGENTS_PLAYER_BUNDLE" ] || qa_fail "Required Unity app bundle does not exist: $QA_MLAGENTS_PLAYER_BUNDLE"
  qa_require_executable "$QA_MLAGENTS_PLAYER_EXECUTABLE"
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
