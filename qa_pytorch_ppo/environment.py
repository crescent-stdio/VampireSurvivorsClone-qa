"""Unity ML-Agents environment adapter for the standalone PPO example."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from mlagents_envs.base_env import ActionTuple
from mlagents_envs.environment import UnityEnvironment
from mlagents_envs.exception import UnityCommunicatorStoppedException
from mlagents_envs.side_channel.engine_configuration_channel import (
    EngineConfigurationChannel,
)

from qa_agent_runtime.presets import QaPreset, format_value, load_preset

OBSERVATION_SIZE = 36
CONTINUOUS_ACTION_SIZE = 2
DISCRETE_BRANCHES = (5,)

TRAINING_PRESET = "train"
EVALUATION_PRESET = "eval"


class EnvironmentContractError(RuntimeError):
    """Raised when Unity does not expose the expected QA behavior contract."""


class EnvironmentInfrastructureError(RuntimeError):
    """Raised when Unity stops before yielding a valid transition."""


@dataclass(frozen=True)
class EnvironmentSpec:
    observation_size: int = OBSERVATION_SIZE
    continuous_size: int = CONTINUOUS_ACTION_SIZE
    discrete_branches: tuple[int, ...] = DISCRETE_BRANCHES


@dataclass(frozen=True)
class HybridAction:
    continuous: np.ndarray
    discrete: np.ndarray


@dataclass(frozen=True)
class StepResult:
    observation: np.ndarray
    reward: float
    terminated: bool
    truncated: bool


class UnityQaEnvironment:
    """Synchronous single-agent adapter over the ML-Agents low-level API."""

    def __init__(
        self,
        *,
        player: Path,
        seed: int,
        evaluation: bool,
        preset: QaPreset | None = None,
        environment_factory: Callable[..., object] = UnityEnvironment,
    ) -> None:
        if not player.is_dir() or player.suffix != ".app":
            raise EnvironmentContractError(
                f"Unity player bundle does not exist: {player}"
            )
        if seed <= 0:
            raise EnvironmentContractError("Seed must be a positive integer.")

        self._preset = preset or load_preset(
            EVALUATION_PRESET if evaluation else TRAINING_PRESET
        )
        time_scale = self._preset.episode.time_scale
        if evaluation and time_scale != 1.0:
            raise EnvironmentContractError(
                "Evaluation must run at time scale 1 for deterministic inference; "
                f"preset {self._preset.name!r} requests {time_scale}."
            )

        arguments = [
            f"-qaPreset={self._preset.name}",
            f"-qaSeed={seed}",
            f"-qaTimeScale={format_value(time_scale)}",
        ]
        if evaluation:
            arguments.insert(0, "-qaMode=evaluate")

        # Unity uses a fixed timestep, so the time scale changes wall-clock duration
        # without changing the trajectory. Without this channel the player runs at 1x,
        # which is why 500k steps took roughly 14 hours while mlagents-learn, which sets
        # the channel itself, finished the same budget in well under an hour.
        self._engine_channel = EngineConfigurationChannel()
        self._environment = environment_factory(
            file_name=str(player),
            seed=seed,
            no_graphics=True,
            timeout_wait=60,
            additional_args=arguments,
            side_channels=[self._engine_channel],
        )
        self._engine_channel.set_configuration_parameters(time_scale=time_scale)
        self._behavior_name = ""
        self._needs_episode_reset = False
        self._closed = False
        try:
            self._environment.reset()
            self._behavior_name = self._validate_behavior()
        except Exception:
            self.close()
            raise

    @property
    def spec(self) -> EnvironmentSpec:
        return EnvironmentSpec()

    @property
    def preset(self) -> QaPreset:
        return self._preset

    def __enter__(self) -> "UnityQaEnvironment":
        return self

    def __exit__(self, _exception_type, _exception, _traceback) -> None:
        self.close()

    def reset(self) -> np.ndarray:
        if self._needs_episode_reset:
            self._advance()
            self._needs_episode_reset = False
        decision_steps, terminal_steps = self._wait_for_steps()
        if len(terminal_steps) > 0:
            raise EnvironmentInfrastructureError(
                "Unity returned a terminal step before an episode reset."
            )
        return self._single_observation(decision_steps)

    def step(self, action: HybridAction) -> StepResult:
        continuous = np.asarray(action.continuous, dtype=np.float32)
        discrete = np.asarray(action.discrete, dtype=np.int32)
        if continuous.shape != (CONTINUOUS_ACTION_SIZE,) or not np.all(
            np.isfinite(continuous)
        ):
            raise EnvironmentContractError(
                "Continuous action must contain exactly 2 finite values."
            )
        if discrete.shape != (1,) or int(discrete[0]) not in range(
            DISCRETE_BRANCHES[0]
        ):
            raise EnvironmentContractError(
                "Discrete action must contain one value in the range 0..4."
            )

        self._environment.set_actions(
            self._behavior_name,
            ActionTuple(continuous=continuous[None, :], discrete=discrete[None, :]),
        )
        self._advance()
        decision_steps, terminal_steps = self._wait_for_steps()
        if len(terminal_steps) > 0:
            if len(terminal_steps) != 1:
                raise EnvironmentContractError(
                    "The QA player must expose exactly one QA agent."
                )
            self._needs_episode_reset = True
            return StepResult(
                observation=self._single_observation(terminal_steps),
                reward=float(terminal_steps.reward[0]),
                terminated=not bool(terminal_steps.interrupted[0]),
                truncated=bool(terminal_steps.interrupted[0]),
            )
        return StepResult(
            observation=self._single_observation(decision_steps),
            reward=float(decision_steps.reward[0]),
            terminated=False,
            truncated=False,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._environment.close()

    def _validate_behavior(self) -> str:
        behavior_names = list(self._environment.behavior_specs)
        if (
            len(behavior_names) != 1
            or behavior_names[0].split("?", 1)[0] != "QaGameplay"
        ):
            raise EnvironmentContractError(
                "The QA player must expose exactly one QaGameplay behavior."
            )
        behavior_name = behavior_names[0]
        behavior_spec = self._environment.behavior_specs[behavior_name]
        observation_specs = behavior_spec.observation_specs
        if len(observation_specs) != 1 or observation_specs[0].shape != (
            OBSERVATION_SIZE,
        ):
            raise EnvironmentContractError(
                "QaGameplay must expose exactly 36 observations."
            )
        action_spec = behavior_spec.action_spec
        if (
            action_spec.continuous_size != CONTINUOUS_ACTION_SIZE
            or tuple(action_spec.discrete_branches) != DISCRETE_BRANCHES
        ):
            raise EnvironmentContractError(
                "QaGameplay must expose 2 continuous actions and one discrete branch of size 5."
            )
        return behavior_name

    def _wait_for_steps(self):
        while True:
            decision_steps, terminal_steps = self._environment.get_steps(
                self._behavior_name
            )
            if len(decision_steps) > 1 or len(terminal_steps) > 1:
                raise EnvironmentContractError(
                    "The QA player must expose exactly one QA agent."
                )
            if len(decision_steps) > 0 or len(terminal_steps) > 0:
                return decision_steps, terminal_steps
            self._advance()

    def _advance(self) -> None:
        try:
            self._environment.step()
        except UnityCommunicatorStoppedException as error:
            raise EnvironmentInfrastructureError(
                "Unity communication stopped before a complete transition was received."
            ) from error

    @staticmethod
    def _single_observation(steps) -> np.ndarray:
        if len(steps) != 1 or len(steps.obs) != 1:
            raise EnvironmentContractError(
                "The QA player must expose exactly one QA agent observation."
            )
        observation = np.asarray(steps.obs[0][0], dtype=np.float32)
        if observation.shape != (OBSERVATION_SIZE,) or not np.all(
            np.isfinite(observation)
        ):
            raise EnvironmentContractError(
                "QA observations must contain exactly 36 finite values."
            )
        return observation.copy()
