"""Versioned checkpoint helpers for the standalone PPO example."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import pickle
from uuid import uuid4

import torch

from qa_pytorch_ppo.environment import EnvironmentSpec
from qa_pytorch_ppo.policy import ActorCritic


FORMAT_VERSION = 1


class CheckpointError(RuntimeError):
    """Raised when a checkpoint cannot be written or validated."""


@dataclass(frozen=True)
class LoadedCheckpoint:
    policy: ActorCritic
    environment_spec: EnvironmentSpec
    seed: int
    global_step: int


def save_checkpoint(
    path: Path,
    *,
    policy: ActorCritic,
    environment_spec: EnvironmentSpec,
    seed: int,
    global_step: int,
    overwrite: bool,
) -> Path:
    """Atomically save a default actor-critic checkpoint."""
    if path.exists() and not overwrite:
        raise CheckpointError(f"Checkpoint already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    payload = {
        "format_version": FORMAT_VERSION,
        "environment_spec": {
            "observation_size": environment_spec.observation_size,
            "continuous_size": environment_spec.continuous_size,
            "discrete_branches": list(environment_spec.discrete_branches),
        },
        "model_config": policy.model_config(),
        "model_state_dict": policy.state_dict(),
        "seed": int(seed),
        "global_step": int(global_step),
    }
    try:
        torch.save(payload, temporary)
        os.replace(temporary, path)
    except OSError as error:
        raise CheckpointError(f"Unable to save checkpoint: {error}") from error
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def load_actor_critic_checkpoint(path: Path, *, device: torch.device) -> LoadedCheckpoint:
    """Load and validate a checkpoint using PyTorch's weights-only mode."""
    try:
        payload = torch.load(path, map_location=device, weights_only=True)
    except (OSError, RuntimeError, ValueError, EOFError, pickle.UnpicklingError) as error:
        raise CheckpointError(f"Unable to load checkpoint: {error}") from error
    if not isinstance(payload, dict) or payload.get("format_version") != FORMAT_VERSION:
        version = payload.get("format_version") if isinstance(payload, dict) else None
        raise CheckpointError(f"Unsupported checkpoint format: {version}")
    try:
        spec_payload = payload["environment_spec"]
        model_config = dict(payload["model_config"])
        model_config["discrete_branches"] = tuple(model_config["discrete_branches"])
        environment_spec = EnvironmentSpec(
            observation_size=int(spec_payload["observation_size"]),
            continuous_size=int(spec_payload["continuous_size"]),
            discrete_branches=tuple(int(value) for value in spec_payload["discrete_branches"]),
        )
        if environment_spec != EnvironmentSpec():
            raise CheckpointError(f"Checkpoint environment contract is incompatible: {environment_spec}")
        policy = ActorCritic(**model_config).to(device)
        policy.load_state_dict(payload["model_state_dict"])
        policy.eval()
        return LoadedCheckpoint(
            policy=policy,
            environment_spec=environment_spec,
            seed=int(payload["seed"]),
            global_step=int(payload["global_step"]),
        )
    except CheckpointError:
        raise
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise CheckpointError(f"Checkpoint payload is invalid: {error}") from error
