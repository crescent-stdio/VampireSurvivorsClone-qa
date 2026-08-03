# QA Gameplay lane

This repository contains an additive, QA-only gameplay lane for deterministic smoke, replay, and ML-Agents PPO workflows. It does not call an LLM or an external API at runtime.

## Architecture

`Assets/Editor/QA/QaAssetGenerator.cs` reproducibly creates and repairs the committed QA assets:

- `Assets/Blueprints/QA/QA Level 1.asset` is a copy of Level 1. Its level duration is 90 seconds, the first miniboss is at 45 seconds, and the original final-boss behavior naturally starts after the level duration.
- Monster spawn-rate curves, spawn-chance arrays, and HP multiplier arrays are copied unchanged. Chest amount and blueprint are retained; delay is scaled proportionally from `30 * (90 / 600)` to `4.5` seconds.
- `Assets/Blueprints/QA/QA Default Chest.asset` copies the original default chest and normalizes every positive loot probability by the original positive total (`1.91`). This preserves relative weights, gives a total of 1, and makes each positive entry reachable. The original chest is deliberately not changed.
- `Assets/Blueprints/QA/QA Main Character.asset` is an isolated copy with smoke-only durability (`10x` health and `100` armor), allowing long-running deterministic episodes to exercise miniboss and final-boss phases without changing the production character.
- `Assets/Scenes/QA/QA Gameplay.unity` is synchronized from Level 1 on every generator run while retaining its QA GUID, then references the QA level. Its `QA Episode` root hosts `QaEpisodeController`, `QaGameplayAgent`, `BehaviorParameters`, and `DecisionRequester`.
- `QaEpisodeController` has execution order `-1000`, so it seeds `CrossSceneData` before `Character.Awake`. The serialized QA-only pause control disables ability-menu pausing; the production dialog keeps its default `PauseOnOpen = true`.

The agent behavior is `QaGameplay`, with exactly 36 vector observations, two continuous movement actions, and one discrete branch of size 5. Unity's fixed timestep is 0.02 seconds (50 Hz), so `DecisionPeriod = 5` yields 10 Hz decisions. There are no visual sensors and no committed inference model; the default path is safe heuristic fallback.

Build scenes are exactly Main Menu (index 0), Level 1 (index 1), then QA Gameplay (index 2). The dedicated QA player build keeps that general order unchanged but passes QA Gameplay as its startup scene, so training and evaluation reach `QaGameplay` immediately.

## Prerequisites

- Unity `2021.3.21f1`; set `UNITY_EDITOR` only when the editor is installed outside the default macOS Hub location.
- Python `3.8.13`; set `PYTHON_BIN` when `python3` is not that exact version.
- ML-Agents Python package `mlagents==0.30.0`. Setup installs it only when explicitly requested. ML-Agents is Apache-2.0 licensed; verify organizational dependency policy before distribution.

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
scripts/qa/replay.sh QAArtifacts/traces/example.json
```

`smoke.sh` runs exactly ten default scripted seeds at an accelerated Unity time scale, exits after one terminal result per player process, and requires at least one recorded final-boss phase. A smoke episode is classified as `TimedOut` after 150 seconds of game time. A provider-neutral POSIX watchdog also terminates a non-responsive player after 60 seconds of wall-clock time by default; override `QA_SMOKE_WALL_TIMEOUT_SECONDS` only for slower hosts. A crash, watchdog timeout, missing unique summary, or incomplete failure artifacts is an infrastructure failure and makes the smoke command exit non-zero. Classified gameplay failures retain the seed, action trace, summary, Unity log, and anomaly screenshot under `QAArtifacts`.

`evaluate.sh` and `replay.sh` use the fixed `-qaSeed=1234`. Evaluation starts `mlagents-learn` with `--resume --inference --seed=1234` before launching the player. Replay accepts a project-relative trace or an explicit absolute file and validates it before launching the player. Pass an episode `summary.json` produced in `QAArtifacts`: it contains the recorded actions, events, and positions. In replay mode the controller feeds those actions through the normal policy path, compares terminal output using `QaReplayComparator`, and exits with code 0 for a match or 1 for a mismatch. Training invokes `mlagents-learn config/qa-ppo.yaml`; the behavior name in that file must remain `QaGameplay`. The default macOS executable is `QAArtifacts/player/QaGameplay.app/Contents/MacOS/project_mgd_vampire`.

Addressables must be built explicitly before creating or training against a player. The source Addressables setting `m_BuildAddressablesWithPlayerBuild` remains disabled by design and is not changed automatically. The local QA player disables Burst compilation because Burst 1.6.6's bundled macOS linker is incompatible with current macOS execution handling; this does not change package versions or project settings.

## Artifacts

Generated outputs are intentionally ignored under `QAArtifacts/`:

- `logs/`: Unity, training, evaluation, and replay logs.
- `TestResults/`: Unity NUnit XML reports.
- `player/`: local Addressables-backed player build.
- `traces/`, `screenshots/`, `checkpoints/`, and `models/`: replay and training products.

## Known source risks

The original Default Chest has total probability 1.91, so later positive entries can be unreachable. This is documented source behavior, not a branch regression; only the QA copy is normalized. The original Level 1 blueprint, original Level 1 scene, package manifest, and package versions are not modified by this lane.
