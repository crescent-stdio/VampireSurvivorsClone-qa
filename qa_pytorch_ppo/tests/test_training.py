import numpy as np
import torch

from qa_pytorch_ppo.environment import EnvironmentSpec, HybridAction, StepResult
from qa_pytorch_ppo.policy import ActorCritic
from qa_pytorch_ppo import ppo


class ShortEpisodeEnvironment:
    spec = EnvironmentSpec()

    def __init__(self):
        self.episode_step = 0
        self.actions = []
        self.reset_calls = 0

    def reset(self):
        self.episode_step = 0
        self.reset_calls += 1
        return np.zeros(36, dtype=np.float32)

    def step(self, action: HybridAction):
        self.actions.append(action)
        self.episode_step += 1
        ended = self.episode_step == 2
        return StepResult(
            observation=np.full(36, self.episode_step / 10.0, dtype=np.float32),
            reward=1.0,
            terminated=ended,
            truncated=False,
        )


def test_train_collects_exact_steps_updates_policy_and_emits_checkpoint_boundaries() -> None:
    torch.manual_seed(12)
    environment = ShortEpisodeEnvironment()
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    before = [parameter.detach().clone() for parameter in model.parameters()]
    checkpoint_steps = []

    result = ppo.train(
        environment,
        model,
        ppo.PpoConfig(
            total_steps=8,
            rollout_steps=4,
            minibatch_size=4,
            update_epochs=1,
            checkpoint_interval=5,
        ),
        device=torch.device("cpu"),
        on_checkpoint=lambda step, _policy: checkpoint_steps.append(step),
    )

    assert result.global_step == 8
    assert result.update_count == 2
    assert len(environment.actions) == 8
    assert environment.reset_calls == 5
    assert checkpoint_steps == [8]
    assert any(not torch.equal(old, new) for old, new in zip(before, model.parameters()))


def test_evaluate_uses_deterministic_actions_until_the_episode_ends() -> None:
    environment = ShortEpisodeEnvironment()
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))

    result = ppo.evaluate(environment, model, device=torch.device("cpu"))

    assert result.steps == 2
    assert result.total_reward == 2.0
