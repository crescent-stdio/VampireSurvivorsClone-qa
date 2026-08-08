from __future__ import annotations

from dataclasses import dataclass
import math

from pydantic import BaseModel, ConfigDict, Field, field_validator

from qa_llm_agent.observation import QaObservation


class InvalidAbilityState(RuntimeError):
    pass


class PolicyAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    movement_x: float = Field(ge=-1.0, le=1.0)
    movement_y: float = Field(ge=-1.0, le=1.0)
    intent: str = Field(min_length=1, max_length=200)

    @field_validator("movement_x", "movement_y")
    @classmethod
    def movement_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Movement must be finite.")
        return value


@dataclass(frozen=True)
class UnityAction:
    continuous: tuple[float, float]
    discrete: tuple[int]
    ability_choice: int
    intent: str
    source: str


def to_unity_action(action: PolicyAction, observation: QaObservation) -> UnityAction:
    movement_x, movement_y = _normalize(action.movement_x, action.movement_y)
    ability_choice = -1
    source = "openai"
    if observation.is_ability_selection_open:
        ability_choice = _first_valid_ability(observation.ability_choices)
        source = "local_safety"
    return UnityAction(
        continuous=(movement_x, movement_y),
        discrete=(ability_choice + 1,),
        ability_choice=ability_choice,
        intent=action.intent,
        source=source,
    )


def _normalize(x: float, y: float) -> tuple[float, float]:
    magnitude = math.hypot(x, y)
    if magnitude <= 1.0:
        return x, y
    return x / magnitude, y / magnitude


def _first_valid_ability(choices: tuple[bool, bool, bool, bool]) -> int:
    for index, available in enumerate(choices):
        if available:
            return index
    raise InvalidAbilityState("The ability dialog is open without a valid ability slot.")
