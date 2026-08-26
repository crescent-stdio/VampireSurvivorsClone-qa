from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .charter import DEFAULT_OBJECTIVE, TestCharter


DEFAULT_SCENARIO_PATH = Path(__file__).resolve().parents[1] / "config" / "qa-scenarios.json"
DEFAULT_V4_SCENARIO_PATH = Path(__file__).resolve().parents[1] / "config" / "qa-scenarios-v4.json"
DEFAULT_V4_GROUND_TRUTH_PATH = Path(__file__).resolve().parents[1] / "config" / "qa-ground-truth-v4.json"

REGISTERED_COVERAGE_TARGETS = frozenset(
    {
        "observe_player_state",
        "observe_relative_positions",
        "select_upgrade",
        "collect_chest",
        "reach_multiple_level_ups",
        "restart_after_progress",
        "observe_valid_state",
        "complete_normal_transitions",
        "sustain_long_progression",
    }
)

REGISTERED_ORACLES = frozenset(
    {
        "health_ratio_consistency",
        "relative_position_consistency",
        "upgrade_effect",
        "chest_state_transition",
        "experience_conservation",
        "restart_currency_isolation",
        "valid_observation",
        "normal_state_transitions",
        "stable_long_progression",
        "hp_decreases_on_hit",
        "view_state_match",
        "item_effect_applied",
        "item_hit_range",
        "exp_conservation",
    }
)

REGISTERED_V4_GOALS = frozenset(
    {
        "player_present AND hit_window_observed",
        "player_present AND player_view_present",
        "item_used AND target_visible",
        "item_used AND targets_in_effect_radius",
        "level >= 3",
        "player_present",
        "upgrade_seen AND chest_collected",
        "level >= 3 OR level_time >= 120",
        "upgrade_close_attempted",
        "movement_sample_observed",
        "level_time >= 75",
        "weapon_attack_window_observed",
        "repeated_contact_damage_observed",
        "projectile_enemy_collision_observed",
    }
)

REGISTERED_V4_ORACLES = frozenset(
    {
        "hp_decreases_on_hit",
        "view_state_match",
        "item_effect_applied",
        "item_hit_range",
        "exp_conservation",
        "valid_observation",
        "normal_state_transitions",
        "stable_long_progression",
        "upgrade_dialog_closes",
        "movement_matches_input",
        "regular_monster_spawning_continues",
        "weapon_cooldown_repeats",
        "contact_damage_respects_cooldown",
        "projectile_enemy_collision_applies",
    }
)


class ScenarioContractError(ValueError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class CharterDefinition(StrictModel):
    objective: str = DEFAULT_OBJECTIVE
    movement_constraint: Literal["free", "east", "west", "north", "south"] = "free"
    max_restarts: int = 1
    focus_areas: list[str] = Field(default_factory=list)
    min_forward_component: float = 0.15
    collect_chests: bool = True
    chest_radius: float = 16.0
    threat_radius: float = 8.0
    survival_weight: float = 1.4
    interrupt_health_ratio: float = 0.30
    interrupt_danger_score: float = 0.85

    def to_charter(self) -> TestCharter:
        return TestCharter(
            objective=self.objective,
            movement_constraint=self.movement_constraint,
            max_restarts=self.max_restarts,
            focus_areas=tuple(self.focus_areas),
            min_forward_component=self.min_forward_component,
            collect_chests=self.collect_chests,
            chest_radius=self.chest_radius,
            threat_radius=self.threat_radius,
            survival_weight=self.survival_weight,
            interrupt_health_ratio=self.interrupt_health_ratio,
            interrupt_danger_score=self.interrupt_danger_score,
        )


class ScenarioLimits(StrictModel):
    max_simulation_seconds: float
    max_steps: int

    @model_validator(mode="after")
    def validate_positive_limits(self) -> "ScenarioLimits":
        if self.max_simulation_seconds <= 0:
            raise ValueError("max_simulation_seconds must be greater than zero")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be greater than zero")
        return self


class GroundTruthDefinition(StrictModel):
    bug_id: str | None
    fault_id: str | None
    difficulty: Literal["easy", "medium", "hard"]
    expected_behavior: str
    reproduction_steps: list[str]
    review_status: Literal["pending", "approved"] = "pending"


class Scenario(StrictModel):
    id: str
    difficulty: Literal["easy", "medium", "hard"]
    preset: str
    charter_definition: CharterDefinition = Field(alias="charter")
    seed_set: list[int]
    limits: ScenarioLimits
    coverage_target: str
    oracle: str
    ground_truth: GroundTruthDefinition

    @model_validator(mode="after")
    def validate_contract(self) -> "Scenario":
        if not self.id.strip():
            raise ValueError("scenario id must not be empty")
        if not self.preset.strip():
            raise ValueError("scenario preset must not be empty")
        if not self.seed_set:
            raise ValueError("seed_set must contain at least one seed")
        if len(set(self.seed_set)) != len(self.seed_set):
            raise ValueError("seed_set must not contain duplicate seeds")
        if self.coverage_target not in REGISTERED_COVERAGE_TARGETS:
            raise ValueError(f"unregistered coverage target: {self.coverage_target}")
        if self.oracle not in REGISTERED_ORACLES:
            raise ValueError(f"unregistered oracle: {self.oracle}")
        if self.ground_truth.difficulty != self.difficulty:
            raise ValueError("ground_truth difficulty must match scenario difficulty")
        return self

    @property
    def charter(self) -> TestCharter:
        return self.charter_definition.to_charter()

    def select_seed(self, requested: int | None) -> int:
        seed = self.seed_set[0] if requested is None else requested
        if seed not in self.seed_set:
            raise ScenarioContractError(
                f"seed {seed} is not in scenario {self.id!r} seed_set {self.seed_set}"
            )
        return seed


class ScenarioDocument(StrictModel):
    schema_version: Literal["qa-scenarios/v1"] = Field(alias="schema")
    scenarios: list[Scenario]

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "ScenarioDocument":
        identifiers = [scenario.id for scenario in self.scenarios]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("scenario ids must be unique")
        return self


class V4GoalDefinition(StrictModel):
    description: str
    reached_when: str
    verified_path: str

    @property
    def verified_seed(self) -> int:
        prefix = "scripted:seed="
        if not self.verified_path.startswith(prefix):
            raise ValueError("verified_path must use scripted:seed=<n>")
        try:
            return int(self.verified_path[len(prefix) :])
        except ValueError as error:
            raise ValueError("verified_path must contain an integer seed") from error


class V4OracleDefinition(StrictModel):
    id: str
    kind: Literal["transition", "invariant", "trajectory"]


class V4Scenario(StrictModel):
    id: str
    difficulty: Literal["easy", "medium", "hard"]
    bug_type: Literal["logic_error", "description_flaw", "data_inconsistency", "control"]
    preset: str
    legacy_scenario_id: str
    goal: V4GoalDefinition
    oracle: V4OracleDefinition
    abort: list[str] = Field(default_factory=list)
    mode: Literal["explore", "qa"] = "qa"
    context_refs: list[str] = Field(default_factory=list)
    driver: Literal["scripted", "random", "ppo", "llm"] = "scripted"
    seeds: list[int]
    limits: ScenarioLimits

    @model_validator(mode="after")
    def validate_contract(self) -> "V4Scenario":
        if not self.id.strip() or not self.preset.strip():
            raise ValueError("v4 scenario id and preset must not be empty")
        if not self.seeds or len(set(self.seeds)) != len(self.seeds):
            raise ValueError("v4 scenario seeds must be unique and non-empty")
        if self.goal.reached_when not in REGISTERED_V4_GOALS:
            raise ValueError(f"unregistered v4 goal: {self.goal.reached_when}")
        if self.oracle.id not in REGISTERED_V4_ORACLES:
            raise ValueError(f"unregistered v4 oracle: {self.oracle.id}")
        if self.goal.verified_seed not in self.seeds:
            raise ValueError("verified_path seed must be included in seeds")
        if not self.legacy_scenario_id.strip():
            raise ValueError("legacy_scenario_id must not be empty")
        return self

    def select_seed(self, requested: int) -> int:
        if requested not in self.seeds:
            raise ScenarioContractError(
                f"seed {requested} is not in v4 scenario {self.id!r} seeds {self.seeds}"
            )
        return requested


class V4ScenarioDocument(StrictModel):
    schema_version: Literal["qa-scenarios/v4"] = Field(alias="schema")
    scenarios: list[V4Scenario]

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "V4ScenarioDocument":
        identifiers = [scenario.id for scenario in self.scenarios]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("v4 scenario ids must be unique")
        return self


def load_scenarios(path: Path | None = None) -> list[Scenario]:
    source = path or DEFAULT_SCENARIO_PATH
    try:
        document = ScenarioDocument.model_validate_json(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ScenarioContractError(f"invalid scenario configuration {source}: {error}") from error
    return document.scenarios


def load_scenario(scenario_id: str, path: Path | None = None) -> Scenario:
    scenarios = load_scenarios(path)
    try:
        return next(scenario for scenario in scenarios if scenario.id == scenario_id)
    except StopIteration as error:
        raise ScenarioContractError(f"unknown scenario: {scenario_id}") from error


def load_v4_scenarios(path: Path | None = None) -> list[V4Scenario]:
    source = path or DEFAULT_V4_SCENARIO_PATH
    try:
        document = V4ScenarioDocument.model_validate_json(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ScenarioContractError(f"invalid v4 scenario configuration {source}: {error}") from error
    return document.scenarios


def load_v4_ground_truth(path: Path | None = None) -> dict[str, dict[str, object]]:
    source = path or DEFAULT_V4_GROUND_TRUTH_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioContractError(f"invalid v4 ground truth configuration {source}: {error}") from error
    scenarios = payload.get("scenarios") if isinstance(payload, dict) else None
    if not isinstance(scenarios, dict):
        raise ScenarioContractError("v4 ground truth scenarios must be an object")
    return {
        str(scenario_id): dict(value)
        for scenario_id, value in scenarios.items()
        if isinstance(value, dict)
    }


def canonical_scenario_json(scenario: Scenario) -> str:
    return json.dumps(
        scenario.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def scenario_fingerprint(scenario: Scenario) -> str:
    return hashlib.sha256(canonical_scenario_json(scenario).encode("utf-8")).hexdigest()
