from pathlib import Path

import pytest
import torch

from qa_pytorch_ppo import checkpoint
from qa_pytorch_ppo.environment import EnvironmentSpec
from qa_pytorch_ppo.policy import ActorCritic


def test_checkpoint_round_trip_restores_model_contract_and_metadata(tmp_path: Path) -> None:
    torch.manual_seed(11)
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    path = tmp_path / "checkpoint-final.pt"
    checkpoint.save_checkpoint(
        path,
        policy=model,
        environment_spec=EnvironmentSpec(),
        seed=42,
        global_step=128,
        overwrite=False,
    )

    loaded = checkpoint.load_actor_critic_checkpoint(path, device=torch.device("cpu"))

    observations = torch.randn((2, 36))
    assert loaded.seed == 42
    assert loaded.global_step == 128
    assert loaded.environment_spec == EnvironmentSpec()
    assert torch.equal(
        model.act(observations, deterministic=True).continuous,
        loaded.policy.act(observations, deterministic=True).continuous,
    )


def test_checkpoint_refuses_a_collision_without_overwrite(tmp_path: Path) -> None:
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    path = tmp_path / "checkpoint-final.pt"
    checkpoint.save_checkpoint(
        path,
        policy=model,
        environment_spec=EnvironmentSpec(),
        seed=42,
        global_step=1,
        overwrite=False,
    )

    with pytest.raises(checkpoint.CheckpointError, match="already exists"):
        checkpoint.save_checkpoint(
            path,
            policy=model,
            environment_spec=EnvironmentSpec(),
            seed=42,
            global_step=2,
            overwrite=False,
        )


def test_checkpoint_rejects_an_unknown_format(tmp_path: Path) -> None:
    path = tmp_path / "unknown.pt"
    torch.save({"format_version": 999}, path)

    with pytest.raises(checkpoint.CheckpointError, match="Unsupported checkpoint format"):
        checkpoint.load_actor_critic_checkpoint(path, device=torch.device("cpu"))


def test_checkpoint_rejects_a_corrupted_file(tmp_path: Path) -> None:
    path = tmp_path / "corrupted.pt"
    path.write_bytes(b"not a pytorch checkpoint")

    with pytest.raises(checkpoint.CheckpointError, match="Unable to load checkpoint"):
        checkpoint.load_actor_critic_checkpoint(path, device=torch.device("cpu"))


def test_checkpoint_rejects_an_incompatible_environment_contract(tmp_path: Path) -> None:
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    path = tmp_path / "incompatible.pt"
    checkpoint.save_checkpoint(
        path,
        policy=model,
        environment_spec=EnvironmentSpec(observation_size=35),
        seed=42,
        global_step=1,
        overwrite=False,
    )

    with pytest.raises(checkpoint.CheckpointError, match="environment contract is incompatible"):
        checkpoint.load_actor_critic_checkpoint(path, device=torch.device("cpu"))
