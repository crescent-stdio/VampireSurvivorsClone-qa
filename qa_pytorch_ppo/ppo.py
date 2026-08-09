"""Small, readable PPO implementation for the Unity QA example."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import torch
from torch import Tensor
from torch.nn import functional

from qa_pytorch_ppo.environment import HybridAction
from qa_pytorch_ppo.policy import PpoPolicy


@dataclass(frozen=True)
class PpoConfig:
    total_steps: int = 500_000
    rollout_steps: int = 2_048
    minibatch_size: int = 256
    update_epochs: int = 3
    learning_rate: float = 3e-4
    gamma: float = 0.99
    gae_lambda: float = 0.95
    clip_epsilon: float = 0.2
    value_coefficient: float = 0.5
    entropy_coefficient: float = 0.005
    max_grad_norm: float = 0.5
    checkpoint_interval: int = 50_000

    def __post_init__(self) -> None:
        if min(
            self.total_steps,
            self.rollout_steps,
            self.minibatch_size,
            self.update_epochs,
            self.checkpoint_interval,
        ) <= 0:
            raise ValueError("PPO step, batch, epoch, and checkpoint values must be positive.")
        if self.minibatch_size > self.rollout_steps:
            raise ValueError("PPO minibatch size cannot exceed rollout steps.")
        if self.learning_rate <= 0 or self.max_grad_norm <= 0:
            raise ValueError("Learning rate and max gradient norm must be positive.")
        if not 0 < self.gamma <= 1 or not 0 <= self.gae_lambda <= 1:
            raise ValueError("Gamma and GAE lambda must be in their probability ranges.")
        if self.clip_epsilon <= 0:
            raise ValueError("PPO clip epsilon must be positive.")


@dataclass(frozen=True)
class RolloutBatch:
    observations: Tensor
    pre_tanh_continuous: Tensor
    discrete: Tensor
    old_log_probs: Tensor
    advantages: Tensor
    returns: Tensor


@dataclass(frozen=True)
class PpoMetrics:
    policy_loss: float
    value_loss: float
    entropy: float
    approximate_kl: float
    clip_fraction: float


@dataclass(frozen=True)
class TrainingResult:
    global_step: int
    update_count: int
    last_metrics: PpoMetrics


@dataclass(frozen=True)
class EvaluationResult:
    steps: int
    total_reward: float


def compute_gae(
    *,
    rewards: Tensor,
    values: Tensor,
    next_values: Tensor,
    episode_ends: Tensor,
    gamma: float,
    gae_lambda: float,
) -> tuple[Tensor, Tensor]:
    """Compute GAE while stopping recursion at every episode boundary."""
    if not (
        rewards.shape == values.shape == next_values.shape == episode_ends.shape
        and rewards.ndim == 1
    ):
        raise ValueError("GAE inputs must be one-dimensional tensors of equal length.")
    advantages = torch.zeros_like(rewards)
    running_advantage = torch.zeros((), dtype=rewards.dtype, device=rewards.device)
    for index in range(rewards.shape[0] - 1, -1, -1):
        delta = rewards[index] + gamma * next_values[index] - values[index]
        continues = (~episode_ends[index]).to(dtype=rewards.dtype)
        running_advantage = delta + gamma * gae_lambda * continues * running_advantage
        advantages[index] = running_advantage
    return advantages, advantages + values


class PpoTrainer:
    def __init__(self, policy: PpoPolicy, config: PpoConfig, *, device: torch.device) -> None:
        self.policy = policy.to(device)
        self.config = config
        self.device = device
        self.optimizer = torch.optim.Adam(self.policy.parameters(), lr=config.learning_rate)

    def update(self, batch: RolloutBatch) -> PpoMetrics:
        observations = batch.observations.to(self.device)
        pre_tanh = batch.pre_tanh_continuous.to(self.device)
        discrete = batch.discrete.to(self.device)
        old_log_probs = batch.old_log_probs.to(self.device)
        returns = batch.returns.to(self.device)
        advantages = batch.advantages.to(self.device)
        advantages = (advantages - advantages.mean()) / (
            advantages.std(unbiased=False) + 1e-8
        )

        totals = torch.zeros(5, dtype=torch.float64)
        minibatch_count = 0
        sample_count = observations.shape[0]
        for _ in range(self.config.update_epochs):
            indices = torch.randperm(sample_count, device=self.device)
            for start in range(0, sample_count, self.config.minibatch_size):
                minibatch = indices[start : start + self.config.minibatch_size]
                evaluated = self.policy.evaluate_actions(
                    observations[minibatch],
                    pre_tanh[minibatch],
                    discrete[minibatch],
                )
                log_ratio = evaluated.log_prob - old_log_probs[minibatch]
                ratio = log_ratio.exp()
                clipped_ratio = ratio.clamp(
                    1.0 - self.config.clip_epsilon,
                    1.0 + self.config.clip_epsilon,
                )
                policy_loss = -torch.minimum(
                    ratio * advantages[minibatch],
                    clipped_ratio * advantages[minibatch],
                ).mean()
                value_loss = functional.mse_loss(evaluated.value, returns[minibatch])
                entropy = evaluated.entropy.mean()
                loss = (
                    policy_loss
                    + self.config.value_coefficient * value_loss
                    - self.config.entropy_coefficient * entropy
                )

                self.optimizer.zero_grad(set_to_none=True)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    self.policy.parameters(),
                    self.config.max_grad_norm,
                )
                self.optimizer.step()

                with torch.no_grad():
                    approximate_kl = (-log_ratio).mean()
                    clip_fraction = (
                        (ratio - 1.0).abs() > self.config.clip_epsilon
                    ).float().mean()
                    totals += torch.tensor(
                        [
                            policy_loss.item(),
                            value_loss.item(),
                            entropy.item(),
                            approximate_kl.item(),
                            clip_fraction.item(),
                        ],
                        dtype=torch.float64,
                    )
                    minibatch_count += 1

        averages = (totals / minibatch_count).tolist()
        return PpoMetrics(*averages)


def train(
    environment,
    policy: PpoPolicy,
    config: PpoConfig,
    *,
    device: torch.device,
    on_checkpoint: Callable[[int, PpoPolicy], None] | None = None,
) -> TrainingResult:
    """Collect rollouts and update ``policy`` until ``total_steps`` is reached."""
    trainer = PpoTrainer(policy, config, device=device)
    observation = environment.reset()
    global_step = 0
    update_count = 0
    next_checkpoint = config.checkpoint_interval
    last_metrics = PpoMetrics(0.0, 0.0, 0.0, 0.0, 0.0)

    while global_step < config.total_steps:
        observations = []
        pre_tanh_actions = []
        discrete_actions = []
        old_log_probs = []
        rewards = []
        values = []
        next_values = []
        episode_ends = []
        rollout_size = min(config.rollout_steps, config.total_steps - global_step)

        for _ in range(rollout_size):
            observation_tensor = torch.as_tensor(
                observation,
                dtype=torch.float32,
                device=device,
            ).unsqueeze(0)
            with torch.no_grad():
                action_batch = policy.act(observation_tensor, deterministic=False)
            result = environment.step(
                HybridAction(
                    continuous=action_batch.continuous[0].cpu().numpy().astype(np.float32),
                    discrete=action_batch.discrete[0].cpu().numpy().astype(np.int32),
                )
            )
            episode_end = result.terminated or result.truncated
            with torch.no_grad():
                if result.terminated:
                    next_value = torch.zeros((), dtype=torch.float32, device=device)
                else:
                    next_observation = torch.as_tensor(
                        result.observation,
                        dtype=torch.float32,
                        device=device,
                    ).unsqueeze(0)
                    next_value = policy.value(next_observation)[0]

            observations.append(observation_tensor[0].cpu())
            pre_tanh_actions.append(action_batch.pre_tanh_continuous[0].cpu())
            discrete_actions.append(action_batch.discrete[0].cpu())
            old_log_probs.append(action_batch.log_prob[0].cpu())
            rewards.append(float(result.reward))
            values.append(action_batch.value[0].cpu())
            next_values.append(next_value.cpu())
            episode_ends.append(episode_end)
            global_step += 1
            observation = environment.reset() if episode_end else result.observation

        reward_tensor = torch.tensor(rewards, dtype=torch.float32)
        value_tensor = torch.stack(values)
        next_value_tensor = torch.stack(next_values)
        advantages, returns = compute_gae(
            rewards=reward_tensor,
            values=value_tensor,
            next_values=next_value_tensor,
            episode_ends=torch.tensor(episode_ends, dtype=torch.bool),
            gamma=config.gamma,
            gae_lambda=config.gae_lambda,
        )
        last_metrics = trainer.update(
            RolloutBatch(
                observations=torch.stack(observations),
                pre_tanh_continuous=torch.stack(pre_tanh_actions),
                discrete=torch.stack(discrete_actions),
                old_log_probs=torch.stack(old_log_probs),
                advantages=advantages,
                returns=returns,
            )
        )
        update_count += 1
        if on_checkpoint is not None and global_step >= next_checkpoint:
            on_checkpoint(global_step, policy)
            while next_checkpoint <= global_step:
                next_checkpoint += config.checkpoint_interval

    return TrainingResult(global_step, update_count, last_metrics)


def evaluate(environment, policy: PpoPolicy, *, device: torch.device) -> EvaluationResult:
    """Run one episode with deterministic hybrid actions."""
    policy = policy.to(device)
    policy.eval()
    observation = environment.reset()
    steps = 0
    total_reward = 0.0
    while True:
        observation_tensor = torch.as_tensor(
            observation,
            dtype=torch.float32,
            device=device,
        ).unsqueeze(0)
        with torch.no_grad():
            action_batch = policy.act(observation_tensor, deterministic=True)
        result = environment.step(
            HybridAction(
                continuous=action_batch.continuous[0].cpu().numpy().astype(np.float32),
                discrete=action_batch.discrete[0].cpu().numpy().astype(np.int32),
            )
        )
        steps += 1
        total_reward += result.reward
        if result.terminated or result.truncated:
            return EvaluationResult(steps=steps, total_reward=total_reward)
        observation = result.observation
