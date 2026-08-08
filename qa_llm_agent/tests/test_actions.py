import math

import pytest
from pydantic import ValidationError

from qa_llm_agent.actions import InvalidAbilityState, PolicyAction, to_unity_action
from qa_llm_agent.observation import decode_observation


def observation(*, modal: bool = False, abilities=(False, False, False, False)):
    values = [0.0] * 36
    values[27:31] = [float(value) for value in abilities]
    values[35] = float(modal)
    return decode_observation(values)


def test_action_mapping_normalizes_movement_and_applies_the_discrete_offset() -> None:
    action = PolicyAction.model_construct(movement_x=3.0, movement_y=4.0, intent="collect")

    mapped = to_unity_action(action, observation())

    assert mapped.continuous == pytest.approx((0.6, 0.8))
    assert mapped.ability_choice == -1
    assert mapped.discrete == (0,)
    assert mapped.source == "openai"


def test_local_safety_selects_the_first_valid_ability_without_waiting_for_the_llm() -> None:
    action = PolicyAction(movement_x=0.25, movement_y=-0.5, intent="kite")

    mapped = to_unity_action(action, observation(modal=True, abilities=(False, True, True, False)))

    assert mapped.ability_choice == 1
    assert mapped.discrete == (2,)
    assert mapped.source == "local_safety"


def test_local_safety_rejects_an_open_modal_without_a_valid_slot() -> None:
    with pytest.raises(InvalidAbilityState, match="valid ability"):
        to_unity_action(PolicyAction(movement_x=0, movement_y=0, intent="wait"), observation(modal=True))


def test_policy_action_rejects_non_finite_movement() -> None:
    with pytest.raises(ValidationError):
        PolicyAction(movement_x=math.nan, movement_y=0, intent="invalid")


def test_policy_action_rejects_movement_outside_the_model_contract() -> None:
    with pytest.raises(ValidationError):
        PolicyAction(movement_x=1.01, movement_y=0, intent="invalid")
