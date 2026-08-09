from pathlib import Path

import pytest
import torch

from qa_agent_runtime.presets import load_preset
from qa_pytorch_ppo import checkpoint
from qa_pytorch_ppo.environment import EnvironmentSpec
from qa_pytorch_ppo.policy import ActorCritic


def test_checkpoint_round_trip_restores_model_contract_and_metadata(
    tmp_path: Path,
) -> None:
    torch.manual_seed(11)
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    path = tmp_path / "checkpoint-final.pt"
    checkpoint.save_checkpoint(
        path,
        policy=model,
        environment_spec=EnvironmentSpec(),
        preset=load_preset("train"),
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
        preset=load_preset("train"),
        seed=42,
        global_step=1,
        overwrite=False,
    )

    with pytest.raises(checkpoint.CheckpointError, match="already exists"):
        checkpoint.save_checkpoint(
            path,
            policy=model,
            environment_spec=EnvironmentSpec(),
            preset=load_preset("train"),
            seed=42,
            global_step=2,
            overwrite=False,
        )


def test_checkpoint_rejects_an_unknown_format(tmp_path: Path) -> None:
    path = tmp_path / "unknown.pt"
    torch.save({"format_version": 999}, path)

    with pytest.raises(
        checkpoint.CheckpointError, match="Unsupported checkpoint format"
    ):
        checkpoint.load_actor_critic_checkpoint(path, device=torch.device("cpu"))


def test_checkpoint_rejects_a_corrupted_file(tmp_path: Path) -> None:
    path = tmp_path / "corrupted.pt"
    path.write_bytes(b"not a pytorch checkpoint")

    with pytest.raises(checkpoint.CheckpointError, match="Unable to load checkpoint"):
        checkpoint.load_actor_critic_checkpoint(path, device=torch.device("cpu"))


def test_checkpoint_rejects_an_incompatible_environment_contract(
    tmp_path: Path,
) -> None:
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    path = tmp_path / "incompatible.pt"
    checkpoint.save_checkpoint(
        path,
        policy=model,
        environment_spec=EnvironmentSpec(observation_size=35),
        preset=load_preset("train"),
        seed=42,
        global_step=1,
        overwrite=False,
    )

    with pytest.raises(
        checkpoint.CheckpointError, match="environment contract is incompatible"
    ):
        checkpoint.load_actor_critic_checkpoint(path, device=torch.device("cpu"))


def test_checkpoint_records_the_preset_it_was_trained_under(tmp_path: Path) -> None:
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    path = tmp_path / "train.pt"
    train = load_preset("train")
    checkpoint.save_checkpoint(
        path,
        policy=model,
        environment_spec=EnvironmentSpec(),
        preset=train,
        seed=42,
        global_step=1,
        overwrite=False,
    )

    loaded = checkpoint.load_actor_critic_checkpoint(path, device=torch.device("cpu"))

    assert loaded.preset == "train"
    assert loaded.preset_fingerprint == train.fingerprint


def test_a_training_checkpoint_is_accepted_for_evaluation(tmp_path: Path) -> None:
    # train and eval differ only in time scale, which cannot change a trajectory under a
    # fixed timestep, so a policy must transfer between them.
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    path = tmp_path / "train.pt"
    checkpoint.save_checkpoint(
        path,
        policy=model,
        environment_spec=EnvironmentSpec(),
        preset=load_preset("train"),
        seed=42,
        global_step=1,
        overwrite=False,
    )

    loaded = checkpoint.load_actor_critic_checkpoint(
        path, device=torch.device("cpu"), preset=load_preset("eval")
    )

    assert loaded.preset == "train"


def test_a_smoke_checkpoint_is_rejected_for_evaluation(tmp_path: Path) -> None:
    # EnvironmentSpec cannot catch this: every preset shares the same 36/2/(5,) contract,
    # so without the fingerprint a policy trained against the durable smoke character
    # would load silently and be judged against an environment it never saw.
    model = ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    path = tmp_path / "smoke.pt"
    checkpoint.save_checkpoint(
        path,
        policy=model,
        environment_spec=EnvironmentSpec(),
        preset=load_preset("smoke"),
        seed=42,
        global_step=1,
        overwrite=False,
    )

    with pytest.raises(checkpoint.CheckpointError, match="does not match preset 'eval'"):
        checkpoint.load_actor_critic_checkpoint(
            path, device=torch.device("cpu"), preset=load_preset("eval")
        )


def test_version_one_checkpoints_are_rejected_with_an_actionable_message(tmp_path: Path) -> None:
    path = tmp_path / "legacy.pt"
    torch.save({"format_version": 1}, path)

    with pytest.raises(checkpoint.CheckpointError, match="predate preset recording"):
        checkpoint.load_actor_critic_checkpoint(path, device=torch.device("cpu"))
