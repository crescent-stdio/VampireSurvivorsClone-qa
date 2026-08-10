from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_OBJECTIVE = (
    "Explore autonomously, maximize useful gameplay coverage, survive when possible, "
    "and report only evidence-backed anomalies."
)

MOVEMENT_VECTORS: dict[str, tuple[float, float] | None] = {
    "free": None,
    "east": (1.0, 0.0),
    "west": (-1.0, 0.0),
    "north": (0.0, 1.0),
    "south": (0.0, -1.0),
}


@dataclass(frozen=True)
class TestCharter:
    __test__ = False

    """User-supplied QA objective plus executor-enforced constraints."""

    objective: str = DEFAULT_OBJECTIVE
    movement_constraint: str = "free"
    max_restarts: int = 1
    focus_areas: tuple[str, ...] = field(default_factory=tuple)
    min_forward_component: float = 0.15
    collect_chests: bool = True
    chest_radius: float = 16.0
    threat_radius: float = 8.0
    survival_weight: float = 1.4
    interrupt_health_ratio: float = 0.30
    interrupt_danger_score: float = 0.85

    def __post_init__(self) -> None:
        objective = self.objective.strip()
        if not objective:
            raise ValueError("QA objective must not be empty")
        if len(objective) > 4000:
            raise ValueError("QA objective must be 4000 characters or fewer")
        if self.movement_constraint not in MOVEMENT_VECTORS:
            raise ValueError(f"Unsupported movement constraint: {self.movement_constraint}")
        if self.max_restarts < 0:
            raise ValueError("max_restarts must be zero or greater")
        if len(self.focus_areas) > 20:
            raise ValueError("At most 20 focus areas are allowed")
        if not 0.0 <= self.min_forward_component <= 1.0:
            raise ValueError("min_forward_component must be between 0 and 1")
        if self.chest_radius < 0.0:
            raise ValueError("chest_radius must be zero or greater")
        if self.threat_radius <= 0.0:
            raise ValueError("threat_radius must be greater than zero")
        if self.survival_weight < 0.0:
            raise ValueError("survival_weight must be zero or greater")
        if not 0.0 <= self.interrupt_health_ratio <= 1.0:
            raise ValueError("interrupt_health_ratio must be between 0 and 1")
        if self.interrupt_danger_score < 0.0:
            raise ValueError("interrupt_danger_score must be zero or greater")
        object.__setattr__(self, "objective", objective)
        object.__setattr__(
            self,
            "focus_areas",
            tuple(area.strip() for area in self.focus_areas if area.strip()),
        )

    @property
    def movement_vector(self) -> tuple[float, float] | None:
        return MOVEMENT_VECTORS[self.movement_constraint]

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "constraints": {
                "movement": self.movement_constraint,
                "requested_minimum_forward_component": self.min_forward_component,
                "movement_semantics": "long_term_net_progress",
                "lateral_detours_allowed": True,
                "temporary_backtracking_allowed": True,
                "max_restarts": self.max_restarts,
            },
            "navigation_policy": {
                "collect_nearby_chests": self.collect_chests,
                "chest_radius": self.chest_radius,
                "threat_radius": self.threat_radius,
                "survival_weight": self.survival_weight,
                "interrupt_health_ratio": self.interrupt_health_ratio,
                "interrupt_danger_score": self.interrupt_danger_score,
            },
            "control_policy": {
                "planner_authority": "llm_when_policy_is_llm",
                "automatic_enemy_avoidance": False,
                "automatic_chest_targeting": False,
                "automatic_direction_correction": False,
                "bridge_role": "hold_the_llm_vector_and_detect_events_only",
                "priority_order": ["survive", "collect_reachable_chests", "net_heading_progress", "coverage"],
            },
            "focus_areas": list(self.focus_areas),
        }
