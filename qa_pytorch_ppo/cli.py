"""Command-line training and evaluation for the standalone PPO example."""

from __future__ import annotations

import argparse
from pathlib import Path
import random
import sys
from typing import Callable

import numpy as np
import torch

from qa_agent_runtime.artifacts import EpisodeArtifactError, load_unique_episode_summary
from qa_agent_runtime.presets import PresetError, load_preset
from qa_pytorch_ppo.checkpoint import (
    CheckpointError,
    load_actor_critic_checkpoint,
    save_checkpoint,
)
from qa_pytorch_ppo.environment import (
    EVALUATION_PRESET,
    EnvironmentContractError,
    EnvironmentInfrastructureError,
    UnityQaEnvironment,
)
from qa_pytorch_ppo.policy import ActorCritic
from qa_pytorch_ppo.ppo import PpoConfig, evaluate, train


DEFAULT_PLAYER = Path("QAArtifacts/player/QaGameplay.app")
DEFAULT_OUTPUT_DIR = Path("QAArtifacts/pytorch-ppo")
DEFAULT_CHECKPOINT = DEFAULT_OUTPUT_DIR / "checkpoint-final.pt"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone PyTorch PPO example for Unity gameplay QA.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    train_parser = subparsers.add_parser("train", help="Train a standalone PyTorch PPO policy.")
    _add_runtime_arguments(train_parser, default_seed=42)
    train_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    train_parser.add_argument("--overwrite", action="store_true")
    train_parser.add_argument("--hidden-units", type=int, default=128)
    train_parser.add_argument("--num-layers", type=int, default=2)
    train_parser.add_argument("--total-steps", type=int, default=500_000)
    train_parser.add_argument("--rollout-steps", type=int, default=2_048)
    train_parser.add_argument("--minibatch-size", type=int, default=256)
    train_parser.add_argument("--update-epochs", type=int, default=3)
    train_parser.add_argument("--learning-rate", type=float, default=3e-4)
    train_parser.add_argument("--gamma", type=float, default=0.99)
    train_parser.add_argument("--gae-lambda", type=float, default=0.95)
    train_parser.add_argument("--clip-epsilon", type=float, default=0.2)
    train_parser.add_argument("--value-coefficient", type=float, default=0.5)
    train_parser.add_argument("--entropy-coefficient", type=float, default=0.005)
    train_parser.add_argument("--max-grad-norm", type=float, default=0.5)
    train_parser.add_argument("--checkpoint-interval", type=int, default=50_000)

    evaluate_parser = subparsers.add_parser("evaluate", help="Evaluate one deterministic episode.")
    _add_runtime_arguments(evaluate_parser, default_seed=1234)
    evaluate_parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    evaluate_parser.add_argument(
        "--artifact-root",
        type=Path,
        help="Unity episode artifact directory; defaults beside the macOS player bundle.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    environment_factory: Callable[..., object] = UnityQaEnvironment,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        device = _resolve_device(args.device)
        _seed_everything(args.seed)
        if args.command == "train":
            return _train_command(args, device, environment_factory)
        return _evaluate_command(args, device, environment_factory)
    except (
        CheckpointError,
        EnvironmentContractError,
        EnvironmentInfrastructureError,
        EpisodeArtifactError,
        PresetError,
        OSError,
        ValueError,
    ) as error:
        print(f"qa pytorch: {error}", file=sys.stderr)
        return 2


def _add_runtime_arguments(parser: argparse.ArgumentParser, *, default_seed: int) -> None:
    parser.add_argument("--player", type=Path, default=DEFAULT_PLAYER)
    parser.add_argument("--device", choices=("cpu", "mps"), default="cpu")
    parser.add_argument("--seed", type=int, default=default_seed)


def _train_command(args, device: torch.device, environment_factory: Callable[..., object]) -> int:
    if args.seed <= 0:
        raise ValueError("Seed must be a positive integer.")
    if not args.overwrite and any(args.output_dir.glob("checkpoint-*.pt")):
        raise CheckpointError(f"Checkpoint already exists in output directory: {args.output_dir}")
    config = PpoConfig(
        total_steps=args.total_steps,
        rollout_steps=args.rollout_steps,
        minibatch_size=args.minibatch_size,
        update_epochs=args.update_epochs,
        learning_rate=args.learning_rate,
        gamma=args.gamma,
        gae_lambda=args.gae_lambda,
        clip_epsilon=args.clip_epsilon,
        value_coefficient=args.value_coefficient,
        entropy_coefficient=args.entropy_coefficient,
        max_grad_norm=args.max_grad_norm,
        checkpoint_interval=args.checkpoint_interval,
    )
    policy = ActorCritic(
        observation_size=36,
        continuous_size=2,
        discrete_branches=(5,),
        hidden_units=args.hidden_units,
        num_layers=args.num_layers,
    )

    with environment_factory(
        player=args.player,
        seed=args.seed,
        evaluation=False,
    ) as environment:
        def checkpoint_callback(global_step, current_policy) -> None:
            save_checkpoint(
                args.output_dir / f"checkpoint-step-{global_step:09d}.pt",
                policy=current_policy,
                environment_spec=environment.spec,
                preset=environment.preset,
                seed=args.seed,
                global_step=global_step,
                overwrite=args.overwrite,
            )

        result = train(
            environment,
            policy,
            config,
            device=device,
            on_checkpoint=checkpoint_callback,
        )
        save_checkpoint(
            args.output_dir / "checkpoint-final.pt",
            policy=policy,
            environment_spec=environment.spec,
            preset=environment.preset,
            seed=args.seed,
            global_step=result.global_step,
            overwrite=args.overwrite,
        )
    metrics = result.last_metrics
    print(
        f"steps={result.global_step} updates={result.update_count} "
        f"policy_loss={metrics.policy_loss:.6f} value_loss={metrics.value_loss:.6f} "
        f"entropy={metrics.entropy:.6f} approximate_kl={metrics.approximate_kl:.6f} "
        f"clip_fraction={metrics.clip_fraction:.6f}"
    )
    return 0


def _evaluate_command(args, device: torch.device, environment_factory: Callable[..., object]) -> int:
    if args.seed <= 0:
        raise ValueError("Seed must be a positive integer.")
    artifact_root = args.artifact_root or args.player.parent / "QAArtifacts"
    if any(artifact_root.glob(f"episode-{args.seed:08d}-*")):
        raise EpisodeArtifactError(f"An episode for seed {args.seed} already exists.")
    evaluation_preset = load_preset(EVALUATION_PRESET)
    loaded = load_actor_critic_checkpoint(
        args.checkpoint, device=device, preset=evaluation_preset
    )
    evaluation_error = None
    try:
        with environment_factory(
            player=args.player,
            seed=args.seed,
            evaluation=True,
        ) as environment:
            result = evaluate(environment, loaded.policy, device=device)
    except EnvironmentInfrastructureError as error:
        evaluation_error = error
        result = None

    try:
        summary = load_unique_episode_summary(artifact_root, args.seed)
    except EpisodeArtifactError:
        if evaluation_error is not None:
            raise evaluation_error
        raise
    outcome = summary.payload.get("Outcome")
    steps = result.steps if result is not None else 0
    total_reward = result.total_reward if result is not None else 0.0
    print(
        f"seed={args.seed} outcome={outcome} steps={steps} "
        f"total_reward={total_reward:.6f} summary={summary.path}"
    )
    return 0 if outcome in (1, "Passed") else 1


def _resolve_device(value: str) -> torch.device:
    if value == "mps" and not torch.backends.mps.is_available():
        raise ValueError("MPS is not available in the selected PyTorch environment. Use cpu.")
    return torch.device(value)


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


if __name__ == "__main__":
    raise SystemExit(main())
