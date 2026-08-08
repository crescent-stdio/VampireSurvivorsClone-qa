import json
from pathlib import Path

import numpy as np
import pytest
from mlagents_envs.exception import UnityCommunicatorStoppedException

from qa_llm_agent.actions import PolicyAction
from qa_llm_agent.async_driver import AsyncPolicyDriver
from qa_llm_agent.policy import PolicyResult, TokenUsage
from qa_llm_agent.runner import RunnerConfig, RunnerConfigurationError, run_episode
from qa_llm_agent.scheduler import DecisionScheduler


class ImmediatePolicy:
    def decide(self, state, trigger, requested_tick):
        return PolicyResult(
            action=PolicyAction(movement_x=0.5, movement_y=0, intent="advance"),
            response_id="resp",
            latency_seconds=0.01,
            usage=TokenUsage(1, 1, 2),
            attempt_count=1,
            trigger=trigger,
            requested_tick=requested_tick,
        )


class Steps:
    def __init__(self, observations):
        self.obs = [np.asarray(observations, dtype=np.float32)]

    def __len__(self):
        return len(self.obs[0])


class FakeEnvironment:
    def __init__(self, artifact_root: Path, seed: int, outcome: int = 1, **kwargs):
        self.artifact_root = artifact_root
        self.seed = seed
        self.outcome = outcome
        self.kwargs = kwargs
        self.behavior_specs = {"QaGameplay?team=0": object()}
        self.actions = []
        self.closed = False

    def reset(self):
        return None

    def get_steps(self, behavior_name):
        return Steps([[0.0] * 36]), Steps([])

    def set_actions(self, behavior_name, action):
        self.actions.append(action)

    def step(self):
        episode = self.artifact_root / f"episode-{self.seed:08d}-fake"
        episode.mkdir(parents=True)
        (episode / "summary.json").write_text(
            json.dumps({"Seed": self.seed, "Outcome": self.outcome, "FailureReason": ""}),
            encoding="utf-8",
        )
        raise UnityCommunicatorStoppedException("normal player shutdown")

    def close(self):
        self.closed = True


def test_runner_treats_communicator_stop_as_normal_when_one_summary_exists(tmp_path: Path) -> None:
    player = tmp_path / "QaGameplay.app"
    player.mkdir()
    artifact_root = tmp_path / "QAArtifacts"
    environments = []

    def factory(**kwargs):
        environment = FakeEnvironment(artifact_root=artifact_root, **kwargs)
        environments.append(environment)
        return environment

    config = RunnerConfig(seed=9301, player=player, artifact_root=artifact_root, watchdog_seconds=5)
    result = run_episode(
        config,
        driver=AsyncPolicyDriver(ImmediatePolicy()),
        scheduler=DecisionScheduler(),
        environment_factory=factory,
    )

    assert result.exit_code == 0
    assert result.summary["Outcome"] == 1
    assert environments[0].kwargs["additional_args"] == [
        "-qaMode=llm",
        "-qaSeed=9301",
        "-qaTimeScale=1",
    ]
    assert environments[0].closed is True
    assert (result.summary_path.parent / "llm-decisions.jsonl").is_file()
    assert (result.summary_path.parent / "llm-run.json").is_file()


def test_runner_returns_one_for_a_classified_unity_failure(tmp_path: Path) -> None:
    player = tmp_path / "QaGameplay.app"
    player.mkdir()
    artifact_root = tmp_path / "QAArtifacts"

    result = run_episode(
        RunnerConfig(seed=9302, player=player, artifact_root=artifact_root),
        driver=AsyncPolicyDriver(ImmediatePolicy()),
        scheduler=DecisionScheduler(),
        environment_factory=lambda **kwargs: FakeEnvironment(
            artifact_root=artifact_root, outcome=4, **kwargs
        ),
    )

    assert result.exit_code == 1
    assert (result.summary_path.parent / "llm-run.json").is_file()


def test_runner_refuses_an_existing_seed_before_starting_unity(tmp_path: Path) -> None:
    player = tmp_path / "QaGameplay.app"
    player.mkdir()
    artifact_root = tmp_path / "QAArtifacts"
    (artifact_root / "episode-00009301-old").mkdir(parents=True)

    with pytest.raises(RunnerConfigurationError, match="already exists"):
        run_episode(
            RunnerConfig(seed=9301, player=player, artifact_root=artifact_root),
            driver=AsyncPolicyDriver(ImmediatePolicy()),
            scheduler=DecisionScheduler(),
            environment_factory=lambda **_: pytest.fail("Unity must not start"),
        )
