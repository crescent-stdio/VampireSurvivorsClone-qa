# QA PPO deterministic evaluation

Use the training configuration and a fixed seed for repeatable inference:

```text
uv run --locked --extra trainer mlagents-learn config/qa-ppo.yaml --run-id=qa-ppo --resume --inference --seed=1234 --torch-device=cpu --env=QAArtifacts/player/QaGameplay.app
```

Launch the Unity QA scene with `-qaSeed=1234` and `Time.timeScale = 1`. `QaEpisodeController` resolves this explicit command-line value before calling `Random.InitState`; an invalid value falls back to the serialized episode seed and logs one warning. Do not attach visual sensors; use the 36-float vector observation and `QaGameplay` behavior name from the training configuration.

Prefer `scripts/qa/evaluate.sh`, which starts the trainer and player with the same seed and guarantees a single terminal episode. Override the default with `QA_EVALUATE_SEED=<positive-integer>`. Install the locked environment with `uv sync --locked --extra trainer`. Python is fixed to `3.10.12`, ML-Agents to `1.1.0`, and PyTorch to `2.8.0`. Use `QA_TORCH_DEVICE=mps` only when the selected PyTorch environment reports MPS as available.
