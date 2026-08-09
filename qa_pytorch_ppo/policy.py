"""Replaceable hybrid-action policy interface and default actor-critic."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import math

import torch
from torch import Tensor, nn
from torch.distributions import Categorical, Normal
from torch.nn import functional as functional


@dataclass(frozen=True)
class PolicyActionBatch:
    continuous: Tensor
    pre_tanh_continuous: Tensor
    discrete: Tensor
    log_prob: Tensor
    value: Tensor


@dataclass(frozen=True)
class PolicyEvaluation:
    log_prob: Tensor
    entropy: Tensor
    value: Tensor


class PpoPolicy(nn.Module, ABC):
    """Interface consumed by the reusable PPO update loop."""

    @abstractmethod
    def act(self, observations: Tensor, deterministic: bool = False) -> PolicyActionBatch:
        raise NotImplementedError

    @abstractmethod
    def evaluate_actions(
        self,
        observations: Tensor,
        pre_tanh_actions: Tensor,
        discrete_actions: Tensor,
    ) -> PolicyEvaluation:
        raise NotImplementedError

    @abstractmethod
    def value(self, observations: Tensor) -> Tensor:
        raise NotImplementedError


class ActorCritic(PpoPolicy):
    """Shared MLP with Gaussian, categorical, and value heads."""

    def __init__(
        self,
        *,
        observation_size: int,
        continuous_size: int,
        discrete_branches: tuple[int, ...],
        hidden_units: int = 128,
        num_layers: int = 2,
    ) -> None:
        super().__init__()
        if observation_size <= 0 or continuous_size <= 0:
            raise ValueError("Observation and continuous action sizes must be positive.")
        if len(discrete_branches) != 1 or discrete_branches[0] <= 0:
            raise ValueError("The example policy supports exactly one discrete action branch.")
        if hidden_units <= 0 or num_layers <= 0:
            raise ValueError("Hidden units and layer count must be positive.")

        layers: list[nn.Module] = []
        input_size = observation_size
        for _ in range(num_layers):
            layers.extend((nn.Linear(input_size, hidden_units), nn.Tanh()))
            input_size = hidden_units
        self.backbone = nn.Sequential(*layers)
        self.continuous_mean = nn.Linear(input_size, continuous_size)
        self.continuous_log_std = nn.Parameter(torch.zeros(continuous_size))
        self.discrete_logits = nn.Linear(input_size, discrete_branches[0])
        self.value_head = nn.Linear(input_size, 1)

        self.observation_size = observation_size
        self.continuous_size = continuous_size
        self.discrete_branches = discrete_branches
        self.hidden_units = hidden_units
        self.num_layers = num_layers

    def act(self, observations: Tensor, deterministic: bool = False) -> PolicyActionBatch:
        features = self.backbone(observations)
        normal, categorical = self._distributions(features)
        pre_tanh = normal.mean if deterministic else normal.rsample()
        discrete = (
            categorical.logits.argmax(dim=-1)
            if deterministic
            else categorical.sample()
        )
        return PolicyActionBatch(
            continuous=torch.tanh(pre_tanh),
            pre_tanh_continuous=pre_tanh,
            discrete=discrete.unsqueeze(-1),
            log_prob=self._joint_log_probability(normal, categorical, pre_tanh, discrete),
            value=self.value_head(features).squeeze(-1),
        )

    def evaluate_actions(
        self,
        observations: Tensor,
        pre_tanh_actions: Tensor,
        discrete_actions: Tensor,
    ) -> PolicyEvaluation:
        features = self.backbone(observations)
        normal, categorical = self._distributions(features)
        discrete = discrete_actions.squeeze(-1)
        return PolicyEvaluation(
            log_prob=self._joint_log_probability(
                normal,
                categorical,
                pre_tanh_actions,
                discrete,
            ),
            entropy=normal.entropy().sum(dim=-1) + categorical.entropy(),
            value=self.value_head(features).squeeze(-1),
        )

    def value(self, observations: Tensor) -> Tensor:
        return self.value_head(self.backbone(observations)).squeeze(-1)

    def model_config(self) -> dict[str, object]:
        return {
            "observation_size": self.observation_size,
            "continuous_size": self.continuous_size,
            "discrete_branches": list(self.discrete_branches),
            "hidden_units": self.hidden_units,
            "num_layers": self.num_layers,
        }

    def _distributions(self, features: Tensor) -> tuple[Normal, Categorical]:
        mean = self.continuous_mean(features)
        log_std = self.continuous_log_std.clamp(-5.0, 2.0).expand_as(mean)
        return Normal(mean, log_std.exp()), Categorical(logits=self.discrete_logits(features))

    @staticmethod
    def _joint_log_probability(
        normal: Normal,
        categorical: Categorical,
        pre_tanh: Tensor,
        discrete: Tensor,
    ) -> Tensor:
        log_jacobian = 2.0 * (
            math.log(2.0) - pre_tanh - functional.softplus(-2.0 * pre_tanh)
        )
        continuous_log_prob = (normal.log_prob(pre_tanh) - log_jacobian).sum(dim=-1)
        return continuous_log_prob + categorical.log_prob(discrete)
