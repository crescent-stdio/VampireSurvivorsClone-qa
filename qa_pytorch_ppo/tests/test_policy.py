import torch

from qa_pytorch_ppo import policy


def test_actor_critic_samples_finite_hybrid_actions_in_the_unity_ranges() -> None:
    torch.manual_seed(7)
    model = policy.ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))

    result = model.act(torch.zeros((4, 36)), deterministic=False)

    assert result.continuous.shape == (4, 2)
    assert result.pre_tanh_continuous.shape == (4, 2)
    assert result.discrete.shape == (4, 1)
    assert result.log_prob.shape == (4,)
    assert result.value.shape == (4,)
    assert torch.all(result.continuous >= -1.0)
    assert torch.all(result.continuous <= 1.0)
    assert torch.all(result.discrete >= 0)
    assert torch.all(result.discrete < 5)
    assert torch.all(torch.isfinite(result.log_prob))
    assert torch.all(torch.isfinite(result.value))


def test_actor_critic_deterministic_action_uses_mean_and_argmax() -> None:
    torch.manual_seed(8)
    model = policy.ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    observations = torch.randn((3, 36))

    first = model.act(observations, deterministic=True)
    second = model.act(observations, deterministic=True)

    assert torch.equal(first.continuous, second.continuous)
    assert torch.equal(first.discrete, second.discrete)
    assert torch.allclose(first.continuous, torch.tanh(first.pre_tanh_continuous))


def test_evaluate_actions_recomputes_the_sampled_joint_log_probability() -> None:
    torch.manual_seed(9)
    model = policy.ActorCritic(observation_size=36, continuous_size=2, discrete_branches=(5,))
    observations = torch.randn((5, 36))
    sampled = model.act(observations, deterministic=False)

    evaluated = model.evaluate_actions(
        observations,
        sampled.pre_tanh_continuous,
        sampled.discrete,
    )

    assert torch.allclose(evaluated.log_prob, sampled.log_prob, atol=1e-5)
    assert evaluated.entropy.shape == (5,)
    assert evaluated.value.shape == (5,)
    assert torch.all(torch.isfinite(evaluated.entropy))
