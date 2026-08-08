from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import math
from typing import Sequence


OBSERVATION_SIZE = 36
POSITION_SCALE = 20.0
LEVEL_SCALE = 100.0
KILL_SCALE = 1000.0
ELAPSED_SECONDS_SCALE = 600.0
MAX_ENEMIES = 8


class LevelPhase(IntEnum):
    EARLY = 0
    MID = 1
    MINIBOSS = 2
    FINAL_BOSS = 3
    COMPLETED = 4


@dataclass(frozen=True)
class QaObservation:
    health_ratio: float
    experience_ratio: float
    player_level: int
    is_player_alive: bool
    enemy_count: int
    enemy_relative_positions: tuple[tuple[float, float], ...]
    collectible_relative_position: tuple[float, float] | None
    chest_relative_position: tuple[float, float] | None
    ability_choices: tuple[bool, bool, bool, bool]
    phase: LevelPhase
    kill_count: int
    elapsed_seconds: float
    damage_ratio: float
    is_ability_selection_open: bool
    lossy_fields: frozenset[str] = field(
        default=frozenset(
            {
                "health_ratio",
                "experience_ratio",
                "damage_ratio",
                "player_level",
                "enemy_relative_positions",
                "kill_count",
                "elapsed_seconds",
            }
        ),
        repr=False,
    )

    def as_dict(self) -> dict[str, object]:
        return {
            "health_ratio": self.health_ratio,
            "experience_ratio": self.experience_ratio,
            "player_level": self.player_level,
            "is_player_alive": self.is_player_alive,
            "enemy_count": self.enemy_count,
            "enemy_relative_positions": self.enemy_relative_positions,
            "collectible_relative_position": self.collectible_relative_position,
            "chest_relative_position": self.chest_relative_position,
            "ability_choices": self.ability_choices,
            "phase": self.phase.name.lower(),
            "kill_count": self.kill_count,
            "elapsed_seconds": self.elapsed_seconds,
            "damage_ratio": self.damage_ratio,
            "is_ability_selection_open": self.is_ability_selection_open,
            "lossy_fields": sorted(self.lossy_fields),
        }


def decode_observation(values: Sequence[float]) -> QaObservation:
    if len(values) != OBSERVATION_SIZE:
        raise ValueError(f"QA observations must contain exactly {OBSERVATION_SIZE} finite values.")
    vector = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("QA observations must contain exactly 36 finite values.")

    enemy_count = _scaled_int(vector[4], MAX_ENEMIES)
    enemies = tuple(
        (vector[5 + index * 2] * POSITION_SCALE, vector[6 + index * 2] * POSITION_SCALE)
        for index in range(enemy_count)
    )
    phase_index = _scaled_int(vector[31], LevelPhase.COMPLETED.value)
    return QaObservation(
        health_ratio=_clamp01(vector[0]),
        experience_ratio=_clamp01(vector[1]),
        player_level=_scaled_int(vector[2], int(LEVEL_SCALE)),
        is_player_alive=_as_bool(vector[3]),
        enemy_count=enemy_count,
        enemy_relative_positions=enemies,
        collectible_relative_position=_target(vector, 21),
        chest_relative_position=_target(vector, 24),
        ability_choices=tuple(_as_bool(vector[index]) for index in range(27, 31)),
        phase=LevelPhase(phase_index),
        kill_count=_scaled_int(vector[32], int(KILL_SCALE)),
        elapsed_seconds=_clamp01(vector[33]) * ELAPSED_SECONDS_SCALE,
        damage_ratio=_clamp01(vector[34]),
        is_ability_selection_open=_as_bool(vector[35]),
    )


def _target(values: tuple[float, ...], presence_index: int) -> tuple[float, float] | None:
    if not _as_bool(values[presence_index]):
        return None
    return (
        values[presence_index + 1] * POSITION_SCALE,
        values[presence_index + 2] * POSITION_SCALE,
    )


def _scaled_int(value: float, scale: int) -> int:
    return int(round(_clamp01(value) * scale))


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _as_bool(value: float) -> bool:
    return value >= 0.5
