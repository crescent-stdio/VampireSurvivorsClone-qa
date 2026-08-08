import math

import pytest

from qa_llm_agent.observation import LevelPhase, decode_observation


def test_decode_observation_matches_the_unity_36_value_contract() -> None:
    values = [0.0] * 36
    values[0:5] = [0.5, 0.25, 0.04, 1.0, 0.25]
    values[5:9] = [1.0, -1.0, -0.25, 0.5]
    values[21:27] = [1.0, 0.5, -0.5, 1.0, -0.25, 0.75]
    values[27:31] = [1.0, 0.0, 1.0, 0.0]
    values[31:36] = [0.5, 0.012, 0.05, 0.25, 1.0]

    observation = decode_observation(values)

    assert observation.health_ratio == 0.5
    assert observation.experience_ratio == 0.25
    assert observation.player_level == 4
    assert observation.enemy_count == 2
    assert observation.enemy_relative_positions == ((20.0, -20.0), (-5.0, 10.0))
    assert observation.collectible_relative_position == (10.0, -10.0)
    assert observation.chest_relative_position == (-5.0, 15.0)
    assert observation.ability_choices == (True, False, True, False)
    assert observation.phase is LevelPhase.MINIBOSS
    assert observation.kill_count == 12
    assert observation.elapsed_seconds == 30.0
    assert observation.damage_ratio == 0.25
    assert observation.is_ability_selection_open is True


def test_decode_observation_exposes_ratios_instead_of_inventing_absolute_health() -> None:
    values = [0.0] * 36
    values[0] = 0.75
    values[1] = 0.5
    values[34] = 0.2

    observation = decode_observation(values)

    assert observation.health_ratio == 0.75
    assert observation.experience_ratio == 0.5
    assert observation.damage_ratio == 0.2
    assert not hasattr(observation, "player_health")
    assert {"health_ratio", "experience_ratio", "damage_ratio"}.issubset(observation.lossy_fields)


@pytest.mark.parametrize("values", [[0.0] * 35, [0.0] * 37])
def test_decode_observation_rejects_wrong_vector_sizes(values: list[float]) -> None:
    with pytest.raises(ValueError, match="36"):
        decode_observation(values)


def test_decode_observation_rejects_non_finite_values() -> None:
    values = [0.0] * 36
    values[8] = math.nan

    with pytest.raises(ValueError, match="finite"):
        decode_observation(values)
