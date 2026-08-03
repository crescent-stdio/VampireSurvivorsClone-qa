#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

qa_require_unity
qa_require_python

QA_VENV="$QA_PROJECT_ROOT/.venv-qa"
if [ ! -x "$QA_VENV/bin/python" ]; then
  "$QA_PYTHON_BIN" -m venv "$QA_VENV"
fi

"$QA_VENV/bin/python" -m pip install "mlagents==0.30.0"
printf '%s\n' "QA setup complete. ML-Agents 0.30.0 is Apache-2.0 licensed; review your dependency policy before distribution."
