#!/bin/sh

set -eu
. "$(dirname -- "$0")/common.sh"

QA_PLAYER=${QA_PLAYER:-$QA_DEFAULT_PLAYER_BUNDLE}
case "${1:-}" in
  ''|-*) ;;
  *) QA_PLAYER=$1; shift ;;
esac

qa_require_uv
qa_configure_torch_device
qa_resolve_mlagents_player "$QA_PLAYER"
cd "$QA_PROJECT_ROOT"
exec "$QA_UV_BIN" run --locked --extra trainer python -m qa_pytorch_ppo.cli \
  train --player "$QA_MLAGENTS_PLAYER_BUNDLE" --device "$QA_TORCH_DEVICE" "$@"
