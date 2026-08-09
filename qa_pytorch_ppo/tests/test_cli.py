import json
from pathlib import Path

import numpy as np
import pytest
import torch

from qa_pytorch_ppo import checkpoint, cli
from qa_agent_runtime.presets import load_preset
from qa_pytorch_ppo.environment import EnvironmentSpec, HybridAction, StepResult
from qa_pytorch_ppo.policy import ActorCritic


class CliEnvironment:
    spec = EnvironmentSpec()

    def __init__(
        self,
        *,
        artifact_root: Path | None = None,
        outcome: int = 1,
        preset_name: str = "train",
        **kwargs,
    ):
        self.preset = load_preset(preset_name)
        self.artifact_root = artifact_root
        self.outcome = outcome
        self.seed = kwargs["seed"]
        self.episode_step = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        self.closed = True

    def reset(self):
        self.episode_step = 0
        return np.zeros(36, dtype=np.float32)

    def step(self, _action: HybridAction):
        self.episode_step += 1
        ended = self.episode_step >= 2
        if ended and self.artifact_root is not None:
            episode = self.artifact_root / f"episode-{self.seed:08d}-fake"
            episode.mkdir(parents=True)
            (episode / "summary.json").write_text(
                json.dumps({"Seed": self.seed, "Outcome": self.outcome}),
                encoding="utf-8",
            )
        return StepResult(
            observation=np.zeros(36, dtype=np.float32),
            reward=10.0 if self.outcome == 1 else -2.0,
            terminated=ended,
            truncated=False,
        )


def player_bundle(tmp_path: Path) -> Path:
    player = tmp_path / "QaGameplay.app"
    player.mkdir()
    return player


def test_train_cli_writes_periodic_and_final_checkpoints(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"

    status = cli.main(
        [
            "train",
            "--player", str(player_bundle(tmp_path)),
            "--device", "cpu",
            "--seed", "42",
            "--total-steps", "4",
            "--rollout-steps", "4",
            "--minibatch-size", "4",
            "--update-epochs", "1",
            "--checkpoint-interval", "4",
            "--output-dir", str(output_dir),
        ],
        environment_factory=CliEnvironment,
    )

    assert status == 0
    assert (output_dir / "checkpoint-step-000000004.pt").is_file()
    assert (output_dir / "checkpoint-final.pt").is_file()


@pytest.mark.parametrize(("outcome", "expected_status"), [(1, 0), (2, 1)])
def test_evaluate_cli_returns_the_unity_summary_outcome(
    tmp_path: Path,
    outcome: int,
    expected_status: int,
) -> None:
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    checkpoint_path = tmp_path / "checkpoint.pt"
    checkpoint.save_checkpoint(
        checkpoint_path,
        policy=model,
        environment_spec=EnvironmentSpec(),
        preset=load_preset("eval"),
        seed=42,
        global_step=4,
        overwrite=False,
    )
    artifact_root = tmp_path / "artifacts"

    status = cli.main(
        [
            "evaluate",
            "--player", str(player_bundle(tmp_path)),
            "--device", "cpu",
            "--seed", "1234",
            "--checkpoint", str(checkpoint_path),
            "--artifact-root", str(artifact_root),
        ],
        environment_factory=lambda **kwargs: CliEnvironment(
            artifact_root=artifact_root,
            outcome=outcome,
            **kwargs,
        ),
    )

    assert status == expected_status


def test_evaluate_cli_returns_two_for_a_missing_checkpoint(tmp_path: Path, capsys) -> None:
    status = cli.main(
        [
            "evaluate",
            "--player", str(player_bundle(tmp_path)),
            "--checkpoint", str(tmp_path / "missing.pt"),
        ],
        environment_factory=CliEnvironment,
    )

    assert status == 2
    assert "Unable to load checkpoint" in capsys.readouterr().err


def test_evaluate_cli_defaults_to_the_player_launch_artifact_root(tmp_path: Path) -> None:
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    checkpoint_path = tmp_path / "checkpoint.pt"
    checkpoint.save_checkpoint(
        checkpoint_path,
        policy=model,
        environment_spec=EnvironmentSpec(),
        preset=load_preset("eval"),
        seed=42,
        global_step=4,
        overwrite=False,
    )
    player = player_bundle(tmp_path)
    artifact_root = player.parent / "QAArtifacts"

    status = cli.main(
        [
            "evaluate",
            "--player", str(player),
            "--device", "cpu",
            "--seed", "1234",
            "--checkpoint", str(checkpoint_path),
        ],
        environment_factory=lambda **kwargs: CliEnvironment(
            artifact_root=artifact_root,
            outcome=1,
            **kwargs,
        ),
    )

    assert status == 0
