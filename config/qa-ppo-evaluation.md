# QA PPO deterministic evaluation

Use the training configuration and a fixed seed for repeatable inference:

```text
mlagents-learn config/qa-ppo.yaml --run-id=qa-ppo-eval --resume --inference --seed=1234
```

Launch the Unity QA scene with `-qaSeed=1234` and `Time.timeScale = 1`. `QaEpisodeController` resolves this explicit command-line value before calling `Random.InitState`; an invalid value falls back to the serialized episode seed and logs one warning. Do not attach visual sensors; use the 36-float vector observation and `QaGameplay` behavior name from the training configuration.
