# QA PPO deterministic evaluation

Use the training configuration and a fixed seed for repeatable inference:

```text
mlagents-learn config/qa-ppo.yaml --run-id=qa-ppo-eval --resume --inference --seed=1234
```

Launch the Unity QA scene with the same episode seed (`1234`) and `Time.timeScale = 1`. Do not attach visual sensors; use the 36-float vector observation and `QaGameplay` behavior name from the training configuration.
