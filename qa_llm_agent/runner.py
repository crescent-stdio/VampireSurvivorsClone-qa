from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import signal
from typing import Callable, Iterator

import numpy as np
from mlagents_envs.base_env import ActionTuple
from mlagents_envs.environment import UnityEnvironment
from mlagents_envs.exception import UnityCommunicatorStoppedException

from qa_agent_runtime.artifacts import EpisodeArtifactError, load_unique_episode_summary
from qa_agent_runtime.player import PlayerError, validate_player
from qa_llm_agent.async_driver import AsyncPolicyDriver
from qa_llm_agent.artifacts import write_episode_artifacts
from qa_llm_agent.observation import decode_observation
from qa_llm_agent.scheduler import DecisionScheduler


class RunnerConfigurationError(RuntimeError):
    pass


class RunnerInfrastructureError(RuntimeError):
    pass


class WatchdogExpired(RunnerInfrastructureError):
    pass


@dataclass(frozen=True)
class RunnerConfig:
    seed: int
    player: Path
    artifact_root: Path = Path("QAArtifacts")
    watchdog_seconds: float = 900.0
    model: str = "gpt-5.6-terra"


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    summary_path: Path
    summary: dict[str, object]


def run_episode(
    config: RunnerConfig,
    *,
    driver: AsyncPolicyDriver,
    scheduler: DecisionScheduler,
    environment_factory: Callable[..., object] = UnityEnvironment,
) -> RunResult:
    environment = None
    try:
        _validate_config(config)
        environment = environment_factory(
            file_name=str(config.player),
            seed=config.seed,
            no_graphics=False,
            timeout_wait=60,
            additional_args=[
                "-qaMode=llm",
                f"-qaSeed={config.seed}",
                "-qaTimeScale=1",
            ],
        )
        with _watchdog(config.watchdog_seconds):
            _drive_environment(environment, driver, scheduler)
    finally:
        if environment is not None:
            environment.close()
        driver.close()

    try:
        episode_summary = load_unique_episode_summary(config.artifact_root, config.seed)
    except EpisodeArtifactError as error:
        raise RunnerInfrastructureError(str(error)) from error
    summary_path = episode_summary.path
    summary = episode_summary.payload
    try:
        write_episode_artifacts(
            summary_path.parent,
            seed=config.seed,
            model=config.model,
            records=driver.decisions(),
            api_attempts=scheduler.attempts,
            summary=summary,
        )
    except OSError as error:
        raise RunnerInfrastructureError(
            f"Unable to write LLM episode artifacts: {error}"
        ) from error
    return RunResult(
        exit_code=0 if summary.get("Outcome") in (1, "Passed") else 1,
        summary_path=summary_path,
        summary=summary,
    )


def _drive_environment(
    environment, driver: AsyncPolicyDriver, scheduler: DecisionScheduler
) -> None:
    environment.reset()
    behavior_names = list(environment.behavior_specs)
    if len(behavior_names) != 1:
        raise RunnerInfrastructureError(
            "The QA player must expose exactly one ML-Agents behavior."
        )
    behavior_name = behavior_names[0]
    tick = 0
    try:
        while True:
            decision_steps, terminal_steps = environment.get_steps(behavior_name)
            if len(terminal_steps) > 0:
                return
            if len(decision_steps) == 0:
                environment.step()
                continue
            if len(decision_steps) != 1:
                raise RunnerInfrastructureError(
                    "The LLM runner supports exactly one QA agent."
                )

            tick += 1
            observation = decode_observation(decision_steps.obs[0][0])
            trigger = scheduler.next_trigger(observation)
            if trigger is not None:
                if not scheduler.can_attempt:
                    raise RunnerInfrastructureError(
                        "OpenAI attempt budget is exhausted."
                    )
                driver.submit(observation, trigger, requested_tick=tick)
            action = driver.action_for(observation, applied_tick=tick)
            environment.set_actions(
                behavior_name,
                ActionTuple(
                    continuous=np.asarray([action.continuous], dtype=np.float32),
                    discrete=np.asarray([action.discrete], dtype=np.int32),
                ),
            )
            environment.step()
    except UnityCommunicatorStoppedException:
        return


def _validate_config(config: RunnerConfig) -> None:
    if config.seed <= 0:
        raise RunnerConfigurationError("Seed must be a positive integer.")
    try:
        validate_player(config.player)
    except PlayerError as error:
        raise RunnerConfigurationError(str(error)) from error
    if config.watchdog_seconds <= 0:
        raise RunnerConfigurationError("Watchdog duration must be positive.")
    existing = list(config.artifact_root.glob(f"episode-{config.seed:08d}-*"))
    if existing:
        raise RunnerConfigurationError(
            f"An episode for seed {config.seed} already exists."
        )


@contextmanager
def _watchdog(seconds: float) -> Iterator[None]:
    if not hasattr(signal, "setitimer"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def expire(_signal_number, _frame):
        raise WatchdogExpired(
            f"LLM episode exceeded the {seconds:g}s wall-clock watchdog."
        )

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
