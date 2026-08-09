# QA PPO deterministic evaluation

Prefer `scripts/qa/evaluate.sh`. It starts the trainer and the player with the same seed, connects them, and guarantees a single terminal episode. Override the default with `QA_EVALUATE_SEED=<positive-integer>`.

The equivalent command pair is:

```text
uv run --locked --extra trainer mlagents-learn config/qa-ppo.yaml --run-id=qa-ppo --resume --inference --seed=1234 --torch-device=cpu
QAArtifacts/player/QaGameplay.app/Contents/MacOS/project_mgd_vampire -batchmode -nographics --mlagents-port 5004 -qaPreset=eval -qaSeed=1234 -qaMode=evaluate -qaTimeScale=1
```

`--mlagents-port` is required. A standalone player reads its port from that argument, and without it `Academy.ReadPortFromArgs` returns `-1` in a non-editor build, so no communicator is created. `BehaviorType.Default` then finds neither a communicator nor an assigned model and quietly falls back to the heuristic policy, which means the run measures `ScriptedQaPolicy` rather than the trained model. `5004` is `mlagents_envs.UnityEnvironment.DEFAULT_EDITOR_PORT`, which `mlagents-learn` listens on when it is not given `--env`. Since that fallback is silent, `-qaMode=evaluate` now ends the episode as `Error` / `NoInferenceSource` when no inference source is present.

`--resume` aborts when the run id has no prior data. `evaluate.sh` checks that the trainer survived before launching the player, because the trainer's output goes to `QAArtifacts/logs/evaluate-trainer.log` while the script returns the player's exit status.

`-qaPreset=eval` selects the evaluation environment. It shares its level timings, character durability and observation scale with `train`, so a policy trained under `train` evaluates correctly, while a checkpoint produced under `smoke` is rejected by its environment fingerprint. The standalone PyTorch path enforces the same check when loading a `.pt`.

Launch the Unity QA scene with `-qaSeed=1234` and `Time.timeScale = 1`. `QaEpisodeController` resolves this explicit command-line value before calling `Random.InitState`; an invalid value falls back to the serialized episode seed and logs one warning. Do not attach visual sensors; use the 36-float vector observation and `QaGameplay` behavior name from the training configuration.

A single episode is a single sample. Use `scripts/qa/evaluate-sweep.sh` to run the preset's seed set and report the distribution before making a claim about a model.

Install the locked environment with `uv sync --locked --extra trainer`. Python is fixed to `3.10.12`, ML-Agents to `1.1.0`, and PyTorch to `2.8.0`. Use `QA_TORCH_DEVICE=mps` only when the selected PyTorch environment reports MPS as available.
