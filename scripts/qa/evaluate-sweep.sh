#!/bin/sh

# Evaluate a trained standalone PPO checkpoint across every seed in the eval preset and
# report the distribution. A single episode is one sample; smoke.sh gives the scripted
# lane ten seeds, and this is the equivalent for a learned policy.

set -eu
. "$(dirname -- "$0")/common.sh"

QA_PLAYER=${QA_PLAYER:-$QA_DEFAULT_PLAYER_BUNDLE}
case "${1:-}" in
  ''|-*) ;;
  *) QA_PLAYER=$1; shift ;;
esac

QA_PRESET=${QA_PRESET:-eval}
QA_SWEEP_CHECKPOINT=${QA_SWEEP_CHECKPOINT:-QAArtifacts/pytorch-ppo/checkpoint-final.pt}

qa_require_uv
qa_configure_torch_device
qa_resolve_mlagents_player "$QA_PLAYER"
cd "$QA_PROJECT_ROOT"

QA_SWEEP_SEEDS=${QA_SWEEP_SEEDS:-$(qa_preset_value "$QA_PRESET" run.seeds)}
# Unity writes relative episode artifacts beside the macOS bundle.
QA_SWEEP_ARTIFACT_ROOT=${QA_SWEEP_ARTIFACT_ROOT:-$(dirname -- "$QA_MLAGENTS_PLAYER_BUNDLE")/QAArtifacts}
qa_require_file "$QA_SWEEP_CHECKPOINT"

QA_SWEEP_TOTAL=0
QA_SWEEP_INFRA_FAILURES=0
for QA_SWEEP_SEED in $QA_SWEEP_SEEDS; do
  QA_SWEEP_STATUS=0
  "$QA_UV_BIN" run --locked --extra trainer python -m qa_pytorch_ppo.cli evaluate \
    --player "$QA_MLAGENTS_PLAYER_BUNDLE" \
    --device "$QA_TORCH_DEVICE" \
    --checkpoint "$QA_SWEEP_CHECKPOINT" \
    --artifact-root "$QA_SWEEP_ARTIFACT_ROOT" \
    --seed "$QA_SWEEP_SEED" || QA_SWEEP_STATUS=$?
  QA_SWEEP_TOTAL=$((QA_SWEEP_TOTAL + 1))
  # 0 passed and 1 is a classified gameplay failure; both are data. 2 means the episode
  # never ran, so aggregating it would understate the model rather than measure it.
  if [ "$QA_SWEEP_STATUS" -ge 2 ]; then
    QA_SWEEP_INFRA_FAILURES=$((QA_SWEEP_INFRA_FAILURES + 1))
    printf '%s\n' "qa: seed $QA_SWEEP_SEED failed before producing an episode (exit $QA_SWEEP_STATUS)." >&2
  fi
done

[ "$QA_SWEEP_INFRA_FAILURES" -eq 0 ] ||
  qa_fail "$QA_SWEEP_INFRA_FAILURES of $QA_SWEEP_TOTAL evaluation seeds never produced an episode."

exec "$QA_UV_BIN" run --locked python -m qa_agent_runtime.sweep \
  --artifact-root "$QA_SWEEP_ARTIFACT_ROOT" \
  --json "$QA_PROJECT_ROOT/QAArtifacts/evaluate-sweep.json" \
  --seeds $QA_SWEEP_SEEDS
