# QA Gameplay lane

This repository contains an additive, QA-only gameplay lane for deterministic smoke, replay, ML-Agents PPO, and an opt-in OpenAI LLM workflow. See the [Korean AI agent QA guide](AI_AGENT_QA_GUIDE.ko.md) for onboarding and operational details, and the [Korean PPO quickstart](PPO_QUICKSTART.ko.md) for connecting PPO from a fresh clone.

## Architecture

`config/qa-presets.json` is the single source of truth for QA environment settings. `Assets/Editor/QA/QaAssetGenerator.cs` turns it into `QaPresetBlueprint` assets so a built player resolves its configuration from an asset instead of a repository file, while shell wrappers and Python agents read the JSON directly. `-qaPreset=<name>` selects one and defaults to `smoke`, so smoke, LLM and replay runs are unaffected.

Three presets ship. `smoke` preserves the regression baseline exactly. `train` and `eval` give the character its source durability, because the durable smoke character clamps every hit to one damage against 1000 HP and makes death unreachable, which leaves PPO's failure reward unused; they differ only in time scale, which cannot change a trajectory under a fixed timestep. Each preset carries an environment fingerprint over its level timings, character durability, deadline and observation scale, recorded in every episode summary and in every standalone checkpoint: `train` and `eval` match so a trained policy transfers, and a `smoke` checkpoint is refused. The shared 36/2/(5,) contract cannot catch that on its own.

Training episodes end at the preset deadline. The agent has no step cap and a pass requires killing the boss that spawns at the level duration, so without it an untrained policy never reached a terminal and PPO saw neither terminal reward.

`Assets/Editor/QA/QaAssetGenerator.cs` reproducibly creates and repairs the committed QA assets:

- `Assets/Blueprints/QA/QA Level 1.asset` is a copy of Level 1. Its level duration is 90 seconds, the first miniboss is at 45 seconds, and the original final-boss behavior naturally starts after the level duration.
- Monster spawn-rate curves, spawn-chance arrays, and HP multiplier arrays are copied unchanged. Chest amount and blueprint are retained; delay is scaled proportionally from `30 * (90 / 600)` to `4.5` seconds.
- `Assets/Blueprints/QA/QA Default Chest.asset` copies the original default chest and normalizes every positive loot probability by the original positive total (`1.91`). This preserves relative weights, gives a total of 1, and makes each positive entry reachable. The original chest is deliberately not changed.
- `Assets/Blueprints/QA/QA Main Character.asset` is an isolated copy with smoke-only durability (`10x` health and `100` armor), allowing long-running deterministic episodes to exercise miniboss and final-boss phases without changing the production character.
- `Assets/Scenes/QA/QA Gameplay.unity` is synchronized from Level 1 on every generator run while retaining its QA GUID, then references the QA level. Its `QA Episode` root hosts `QaEpisodeController`, `QaGameplayAgent`, `BehaviorParameters`, and `DecisionRequester`.
- `QaEpisodeController` has execution order `-1000`, so it seeds `CrossSceneData` before `Character.Awake`. The serialized QA-only pause control disables ability-menu pausing; the production dialog keeps its default `PauseOnOpen = true`.
- External-agent terminal outcomes are delivered synchronously to `QaGameplayAgent`. The agent applies the terminal reward and ends its ML-Agents episode, acknowledges completion, and only then may the controller reload the scene.
- QA-only random-decision recording observes the existing post-decision monster index, loot-table index, and selected ability type. With no QA controller subscriber the recorder is a no-op; it does not add random draws or change selection order.

The agent behavior is `QaGameplay`, with exactly 36 vector observations, two continuous movement actions, and one discrete branch of size 5. Unity's fixed timestep is 0.02 seconds (50 Hz), so `DecisionPeriod = 5` yields 10 Hz decisions. There are no visual sensors and no committed inference model; the default path is safe heuristic fallback.

Build scenes are exactly Main Menu (index 0), Level 1 (index 1), then QA Gameplay (index 2). The dedicated QA player build keeps that general order unchanged but passes QA Gameplay as its startup scene, so training and evaluation reach `QaGameplay` immediately.

## Prerequisites

- Unity `6000.0.80f1`; set `UNITY_EDITOR` only when the editor is installed outside the default macOS Hub location.
- [uv](https://docs.astral.sh/uv/) `0.12.x`; the committed `.python-version` selects Python `3.10.12`, the upper bound supported by ML-Agents 1.1.0.
- PyTorch `2.8.0`, the latest version supported by the pinned ML-Agents trainer. CPU is the default; set `QA_TORCH_DEVICE=mps` only when PyTorch reports MPS as available.
- Locked ML-Agents Python packages. `scripts/qa/setup.sh` runs `uv sync --locked --extra trainer`. ML-Agents is Apache-2.0 licensed; verify organizational dependency policy before distribution.
- `OPENAI_API_KEY` only for the opt-in LLM path. Never store it in repository files or artifacts.

## Operator commands

Run commands from the repository root:

```sh
scripts/qa/generate-assets.sh
scripts/qa/test-contracts.sh
scripts/qa/test-editmode.sh
scripts/qa/test-playmode.sh
scripts/qa/setup.sh
scripts/qa/build-addressables.sh
scripts/qa/build-player.sh
scripts/qa/smoke.sh
scripts/qa/train.sh
scripts/qa/evaluate.sh
scripts/qa/train-pytorch.sh
scripts/qa/evaluate-pytorch.sh
scripts/qa/evaluate-sweep.sh
OPENAI_API_KEY=... scripts/qa/run-llm-agent.sh --seed 9301
scripts/qa/replay.sh QAArtifacts/traces/example.json
```

`smoke.sh` runs exactly ten default scripted seeds at the supported `4x` Unity time scale, exits after one terminal result per player process, and requires at least one recorded final-boss phase. Values above `4x` are rejected explicitly because the controller performs at most four 10 Hz logical ticks per frame. A transient loading-frame backlog may drain over the next frames; a backlog remaining for eight consecutive frames is classified as `ControlBacklogExceeded`, preventing silent long-term drift without unbounded frame work. A smoke episode is classified as `TimedOut` after 150 seconds of game time. A provider-neutral POSIX watchdog also terminates a non-responsive player after 60 seconds of wall-clock time by default; override `QA_SMOKE_WALL_TIMEOUT_SECONDS` only for slower hosts. A crash, watchdog timeout, missing unique summary, or incomplete failure artifacts is an infrastructure failure and makes the smoke command exit non-zero. Classified gameplay failures retain the seed, action trace, summary, Unity log, and anomaly screenshot under `QAArtifacts`.

`evaluate.sh` passes `--mlagents-port` to the player. Without it a standalone build creates no communicator, and `BehaviorType.Default` degrades to the heuristic policy, so the run measures `ScriptedQaPolicy` and reports it as the model's result; evaluation now ends as `Error`/`NoInferenceSource` when neither a communicator nor an assigned model is present. It also aborts when the trainer dies, which `--resume` does whenever the run id has no prior data. A single episode is one sample, so `evaluate-sweep.sh` runs the preset's seed set and reports outcome counts, the final-boss reach rate, and the mean and deviation of kills, level, elapsed time, damage and return.

`evaluate.sh` defaults to seed `1234`; set `QA_EVALUATE_SEED` to another positive integer. It starts `mlagents-learn` with `uv run --locked --extra trainer`, `--resume`, and `--inference`, then launches Unity in the single-episode `evaluate` mode. Replay accepts a project-relative trace or an explicit absolute file and validates it before launching the player. Pass an episode `summary.json` produced in `QAArtifacts`: it contains the seed, recorded actions, events, and positions. Replay loads that seed before `Random.InitState`, feeds recorded actions through the normal policy path, compares terminal output using `QaReplayComparator`, and exits with code 0 for a match or 1 for a mismatch. Legacy direct replay traces without a `Seed` are rejected with an actionable compatibility message; use a current summary artifact. Training invokes `mlagents-learn config/qa-ppo.yaml`; the behavior name in that file must remain `QaGameplay`. Release 23 receives the macOS `.app` bundle as `--env`; the scripts resolve the internal executable only when launching the player directly.

### Standalone PyTorch PPO example

The standalone example makes the Unity environment adapter, policy model, and PPO update loop independently replaceable. It does not call `mlagents-learn` and does not change the existing ML-Agents workflow:

```sh
scripts/qa/train-pytorch.sh
scripts/qa/evaluate-pytorch.sh \
  --checkpoint QAArtifacts/pytorch-ppo/checkpoint-final.pt \
  --seed 1234
```

Training defaults to seed `42`, 500,000 environment steps, 2,048-step rollouts, 256-sample minibatches, and three PPO epochs. Use CLI options such as `--total-steps`, `--rollout-steps`, `--minibatch-size`, `--update-epochs`, `--checkpoint-interval`, and `--output-dir` to run smaller experiments. Both scripts accept an optional `.app` bundle as their first non-option argument and otherwise use `QAArtifacts/player/QaGameplay.app`. CPU is the default; `QA_TORCH_DEVICE=mps` is accepted only when PyTorch reports MPS as available.

Periodic checkpoints are named `checkpoint-step-NNNNNNNNN.pt`; the final checkpoint is `checkpoint-final.pt`. Existing checkpoint names are protected unless `--overwrite` is supplied, and overwrite mode replaces only exact checkpoint files without removing their directory or unrelated content. Evaluation requires an unused positive seed and returns 0 for `Passed`, 1 for a classified gameplay failure, and 2 for configuration, communication, or artifact errors.
On macOS, Unity writes relative episode artifacts beside the `.app` bundle, so evaluation defaults to `<player-parent>/QAArtifacts`; use `--artifact-root` only when the player launch directory is customized.

Team policies subclass the public `PpoPolicy` contract while reusing the environment and training loop:

```python
from pathlib import Path

import torch

from qa_pytorch_ppo import PpoConfig, UnityQaEnvironment, train
from team_policy import TeamPolicy

policy = TeamPolicy(observation_size=36, continuous_size=2, discrete_branches=(5,))
with UnityQaEnvironment(
    player=Path("QAArtifacts/player/QaGameplay.app"),
    seed=42,
    evaluation=False,
) as environment:
    train(environment, policy, PpoConfig(), device=torch.device("cpu"))
```

`TeamPolicy` must implement `act`, `evaluate_actions`, and `value`. The built-in checkpoint loader reconstructs the example `ActorCritic`; a custom model should pair the reusable training loop with its own versioned checkpoint loader. Standalone `.pt` checkpoints are Python LLAPI artifacts. They are not ML-Agents ONNX files and cannot be assigned to Unity `BehaviorParameters`.

Addressables must be built explicitly before creating or training against a player. The source Addressables setting `m_BuildAddressablesWithPlayerBuild` remains disabled by design and is not changed automatically. The local QA player disables Burst compilation because Burst 1.6.6's bundled macOS linker is incompatible with current macOS execution handling; this does not change package versions or project settings.

## Artifacts

Generated outputs are intentionally ignored under `QAArtifacts/`:

- `logs/`: Unity, training, evaluation, and replay logs.
- `TestResults/`: Unity NUnit XML reports.
- `player/`: local Addressables-backed player build.
- `traces/`, `screenshots/`, `checkpoints/`, `models/`, and `pytorch-ppo/`: replay and training products.
- `evaluate-sweep.json`: aggregated multi-seed evaluation report.
- `episode-*` and `llm-failures/`: Unity episode data, buffered LLM decisions, and pre-terminal LLM infrastructure failures.

## Known source risks

The original Default Chest has total probability 1.91, so later positive entries can be unreachable. This is documented source behavior, not a branch regression; only the QA copy is normalized. The original Level 1 blueprint, original Level 1 scene, package manifest, and package versions are not modified by this lane.
