import math

import torch

from qa_pytorch_ppo import policy, ppo


def test_compute_gae_matches_hand_calculated_terminal_returns() -> None:
    advantages, returns = ppo.compute_gae(
        rewards=torch.tensor([1.0, 1.0]),
        values=torch.tensor([0.5, 0.25]),
        next_values=torch.tensor([0.25, 0.0]),
        episode_ends=torch.tensor([False, True]),
        gamma=0.9,
        gae_lambda=0.8,
    )

    assert torch.allclose(advantages, torch.tensor([1.265, 0.75]), atol=1e-6)
    assert torch.allclose(returns, torch.tensor([1.765, 1.0]), atol=1e-6)


def test_compute_gae_bootstraps_truncation_without_crossing_the_episode_boundary() -> None:
    advantages, _ = ppo.compute_gae(
        rewards=torch.tensor([1.0, 2.0]),
        values=torch.tensor([0.2, 0.3]),
        next_values=torch.tensor([0.4, 0.5]),
        episode_ends=torch.tensor([True, True]),
        gamma=0.9,
        gae_lambda=0.8,
    )

    assert torch.allclose(advantages, torch.tensor([1.16, 2.15]), atol=1e-6)


def test_ppo_update_changes_policy_parameters_and_returns_finite_metrics() -> None:
    torch.manual_seed(10)
    model = policy.ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    observations = torch.randn((16, 36))
    with torch.no_grad():
        sampled = model.act(observations, deterministic=False)
    batch = ppo.RolloutBatch(
        observations=observations,
        pre_tanh_continuous=sampled.pre_tanh_continuous,
        discrete=sampled.discrete,
        old_log_probs=sampled.log_prob,
        advantages=torch.linspace(-1.0, 1.0, 16),
        returns=sampled.value + torch.linspace(-0.5, 0.5, 16),
    )
    before = [parameter.detach().clone() for parameter in model.parameters()]
    trainer = ppo.PpoTrainer(
        model,
        ppo.PpoConfig(minibatch_size=8, update_epochs=2),
        device=torch.device("cpu"),
    )

    metrics = trainer.update(batch)

    assert any(not torch.equal(old, new) for old, new in zip(before, model.parameters()))
    assert all(
        math.isfinite(value)
        for value in (
            metrics.policy_loss,
            metrics.value_loss,
            metrics.entropy,
            metrics.approximate_kl,
            metrics.clip_fraction,
        )
    )
