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
    # Per-frame bridge survival assist (--policy hybrid). Off by default so the
    # llm and heuristic policies keep their historical zero-intervention contract.
    bridge_assist: bool = False
    assist_survival_weight: float = 0.6

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
        if not 0.0 <= self.assist_survival_weight <= 5.0:
            raise ValueError("assist_survival_weight must be between 0 and 5")
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
            "navigation_policy": self._navigation_policy(),
            "control_policy": self._control_policy(),
            "focus_areas": list(self.focus_areas),
        }

    def _navigation_policy(self) -> dict[str, Any]:
        policy = {
            "collect_nearby_chests": self.collect_chests,
            "chest_radius": self.chest_radius,
            "threat_radius": self.threat_radius,
            "survival_weight": self.survival_weight,
            "interrupt_health_ratio": self.interrupt_health_ratio,
            "interrupt_danger_score": self.interrupt_danger_score,
        }
        if self.bridge_assist:
            policy["assist_survival_weight"] = self.assist_survival_weight
        return policy

    def _control_policy(self) -> dict[str, Any]:
        """Describe what the bridge actually does to the agent's vector.

        The agent reads this as a system message, so it must stay true: an agent told
        the bridge never touches its vector would attribute an assisted trajectory to
        a game bug and raise a false positive on the agent_detection verdict axis.
        """
        priority_order = ["survive", "collect_reachable_chests", "net_heading_progress", "coverage"]
        if not self.bridge_assist:
            return {
                "planner_authority": "llm_when_policy_is_llm",
                "automatic_enemy_avoidance": False,
                "automatic_chest_targeting": False,
                "automatic_direction_correction": False,
                "bridge_role": "hold_the_llm_vector_and_detect_events_only",
                "priority_order": priority_order,
            }
        priority_order = [
            "survive",
            "urgent_item_use",
            "mission_progress",
            "collection",
            "qa_checks",
        ]
        return {
            "planner_authority": "llm_chooses_the_base_vector_for_every_horizon",
            "automatic_enemy_avoidance": True,
            "automatic_enemy_avoidance_detail": (
                "Every simulation frame the bridge computes escape_vector and danger over "
                f"threat_radius={self.threat_radius} and executes "
                f"normalize(llm_vector + escape_vector * {self.assist_survival_weight} * "
                "clamp01(0.35 + danger)) at the requested magnitude. The llm vector itself "
                "is never replaced or re-aimed."
            ),
            "automatic_chest_targeting": False,
            "automatic_direction_correction": False,
            "bridge_role": "hold_the_llm_vector_with_per_frame_survival_assist_and_detect_events",
            "priority_order": priority_order,
        }
