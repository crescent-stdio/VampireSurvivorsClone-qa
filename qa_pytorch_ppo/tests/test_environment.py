from pathlib import Path

import numpy as np
import pytest
from mlagents_envs.base_env import (
    ActionSpec,
    BehaviorSpec,
    DecisionSteps,
    DimensionProperty,
    ObservationSpec,
    ObservationType,
    TerminalSteps,
)

from qa_pytorch_ppo import environment as environment_module


def observation_spec(size: int = 36) -> ObservationSpec:
    return ObservationSpec(
        shape=(size,),
        dimension_property=(DimensionProperty.NONE,),
        observation_type=ObservationType.DEFAULT,
        name="qa-observation",
    )


def decision(values: np.ndarray, reward: float = 0.0) -> DecisionSteps:
    return DecisionSteps(
        obs=[np.asarray([values], dtype=np.float32)],
        reward=np.asarray([reward], dtype=np.float32),
        agent_id=np.asarray([7], dtype=np.int32),
        action_mask=None,
        group_id=np.asarray([0], dtype=np.int32),
        group_reward=np.asarray([0.0], dtype=np.float32),
    )


def no_decision(spec: BehaviorSpec) -> DecisionSteps:
    return DecisionSteps.empty(spec)


def terminal(
    spec: BehaviorSpec,
    values: np.ndarray,
    *,
    reward: float,
    interrupted: bool,
) -> TerminalSteps:
    return TerminalSteps(
        obs=[np.asarray([values], dtype=np.float32)],
        reward=np.asarray([reward], dtype=np.float32),
        interrupted=np.asarray([interrupted], dtype=bool),
        agent_id=np.asarray([7], dtype=np.int32),
        group_id=np.asarray([0], dtype=np.int32),
        group_reward=np.asarray([0.0], dtype=np.float32),
    )


class FakeUnityEnvironment:
    def __init__(self, events, spec: BehaviorSpec, **kwargs):
        self.events = events
        self.behavior_specs = {"QaGameplay?team=0": spec}
        self.kwargs = kwargs
        self.index = 0
        self.actions = []
        self.reset_calls = 0
        self.step_calls = 0
        self.closed = False

    def reset(self):
        self.reset_calls += 1

    def get_steps(self, behavior_name):
        return self.events[self.index]

    def set_actions(self, behavior_name, actions):
        self.actions.append((behavior_name, actions))

    def step(self):
        self.step_calls += 1
        if self.index + 1 < len(self.events):
            self.index += 1

    def close(self):
        self.closed = True


def qa_spec(observation_size: int = 36) -> BehaviorSpec:
    return BehaviorSpec(
        observation_specs=[observation_spec(observation_size)],
        action_spec=ActionSpec.create_hybrid(2, (5,)),
    )


def player_bundle(tmp_path: Path) -> Path:
    player = tmp_path / "QaGameplay.app"
    player.mkdir()
    return player


def test_environment_maps_actions_and_terminal_steps_then_waits_for_the_next_episode(tmp_path: Path) -> None:
    spec = qa_spec()
    first = np.arange(36, dtype=np.float32) / 36.0
    last = np.full(36, 0.5, dtype=np.float32)
    next_episode = np.full(36, 0.25, dtype=np.float32)
    fake = FakeUnityEnvironment(
        [
            (decision(first), TerminalSteps.empty(spec)),
            (no_decision(spec), terminal(spec, last, reward=10.0, interrupted=False)),
            (decision(next_episode), TerminalSteps.empty(spec)),
        ],
        spec,
    )

    with environment_module.UnityQaEnvironment(
        player=player_bundle(tmp_path),
        seed=42,
        evaluation=False,
        environment_factory=lambda **kwargs: fake,
    ) as environment:
        assert np.array_equal(environment.reset(), first)
        result = environment.step(
            environment_module.HybridAction(
                continuous=np.asarray([0.5, -0.25], dtype=np.float32),
                discrete=np.asarray([3], dtype=np.int32),
            )
        )
        assert np.array_equal(result.observation, last)
        assert result.reward == pytest.approx(10.0)
        assert result.terminated is True
        assert result.truncated is False
        assert np.array_equal(environment.reset(), next_episode)

    _, action_tuple = fake.actions[0]
    assert action_tuple.continuous.shape == (1, 2)
    assert action_tuple.discrete.shape == (1, 1)
    assert fake.closed is True


def test_environment_preserves_interrupted_as_truncated(tmp_path: Path) -> None:
    spec = qa_spec()
    values = np.zeros(36, dtype=np.float32)
    fake = FakeUnityEnvironment(
        [
            (decision(values), TerminalSteps.empty(spec)),
            (no_decision(spec), terminal(spec, values, reward=0.5, interrupted=True)),
        ],
        spec,
    )

    with environment_module.UnityQaEnvironment(
        player=player_bundle(tmp_path),
        seed=42,
        evaluation=False,
        environment_factory=lambda **kwargs: fake,
    ) as environment:
        environment.reset()
        result = environment.step(
            environment_module.HybridAction(np.zeros(2, dtype=np.float32), np.zeros(1, dtype=np.int32))
        )

    assert result.terminated is False
    assert result.truncated is True


def test_environment_rejects_an_incompatible_behavior_contract(tmp_path: Path) -> None:
    wrong_spec = qa_spec(observation_size=35)
    fake = FakeUnityEnvironment([], wrong_spec)

    with pytest.raises(environment_module.EnvironmentContractError, match="36 observations"):
        environment_module.UnityQaEnvironment(
            player=player_bundle(tmp_path),
            seed=42,
            evaluation=False,
            environment_factory=lambda **kwargs: fake,
        )

    assert fake.closed is True


def test_environment_rejects_non_finite_observations(tmp_path: Path) -> None:
    spec = qa_spec()
    values = np.zeros(36, dtype=np.float32)
    values[4] = np.nan
    fake = FakeUnityEnvironment([(decision(values), TerminalSteps.empty(spec))], spec)

    with environment_module.UnityQaEnvironment(
        player=player_bundle(tmp_path),
        seed=42,
        evaluation=False,
        environment_factory=lambda **kwargs: fake,
    ) as environment:
        with pytest.raises(environment_module.EnvironmentContractError, match="finite"):
            environment.reset()
