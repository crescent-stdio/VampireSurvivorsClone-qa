from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from .scenarios import Scenario


Transition = dict[str, Any]
CoverageEvaluator = Callable[[list[Transition]], list[str]]
OracleEvaluator = Callable[[list[Transition]], tuple[bool, list[str], str]]


class EvaluationContractError(ValueError):
    pass


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_refs: list[str]
    detail: str = ""

    @field_validator("evidence_refs")
    @classmethod
    def require_evidence_refs(cls, value: list[str]) -> list[str]:
        if not value or any(not item for item in value):
            raise ValueError("evidence_refs must contain at least one non-empty ID")
        return list(dict.fromkeys(value))


class CoverageResult(EvaluationResult):
    status: Literal["reached", "not_reached"]


class OracleResult(EvaluationResult):
    verdict: Literal["pass", "fail", "not_evaluated"]


def _observation(transition: Transition) -> dict[str, Any]:
    value = transition.get("observation") or {}
    return value if isinstance(value, dict) else {}


def _decision(transition: Transition) -> dict[str, Any]:
    value = transition.get("decision") or {}
    return value if isinstance(value, dict) else {}


def _observation_ref(transition: Transition) -> str:
    return str(_observation(transition).get("observation_id") or "")


def _event(transition: Transition) -> dict[str, Any]:
    value = _observation(transition).get("event_state") or {}
    return value if isinstance(value, dict) else {}


def _event_ref(transition: Transition) -> str:
    return str(_event(transition).get("event_id") or "")


def _refs_for(transition: Transition, include_event: bool = False) -> list[str]:
    refs = [_observation_ref(transition)]
    if include_event:
        refs.append(_event_ref(transition))
    return [reference for reference in refs if reference]


def _available_refs(transitions: list[Transition]) -> set[str]:
    return {
        reference
        for transition in transitions
        for reference in _refs_for(transition, include_event=True)
    }


def _fallback_refs(transitions: list[Transition]) -> list[str]:
    for transition in reversed(transitions):
        refs = _refs_for(transition, include_event=bool(_event(transition).get("type")))
        if refs:
            return refs
    raise EvaluationContractError("evaluation cannot produce evidence_refs from these transitions")


def _validate_refs(transitions: list[Transition], refs: list[str]) -> None:
    available = _available_refs(transitions)
    missing = [reference for reference in refs if reference not in available]
    if missing:
        raise EvaluationContractError(f"unresolvable evidence_refs: {missing}")


def _first_player_state(transitions: list[Transition]) -> list[str]:
    for transition in transitions:
        if (_observation(transition).get("player") or {}).get("present"):
            return _refs_for(transition)
    return []


def _first_relative_position(transitions: list[Transition]) -> list[str]:
    for transition in transitions:
        if ((_observation(transition).get("world") or {}).get("qa_entities") or []):
            return _refs_for(transition)
    return []


def _first_action(transitions: list[Transition], action: str) -> list[str]:
    for transition in transitions:
        if _decision(transition).get("action") == action:
            return _refs_for(transition)
    return []


def _first_event(transitions: list[Transition], event_type: str) -> list[str]:
    for transition in transitions:
        if _event(transition).get("type") == event_type:
            return _refs_for(transition, include_event=True)
    return []


def _multiple_level_ups(transitions: list[Transition]) -> list[str]:
    for transition in transitions:
        player = _observation(transition).get("player") or {}
        if int(player.get("level", 0) or 0) >= 3:
            return _refs_for(transition)
    return []


def _restart_after_progress(transitions: list[Transition]) -> list[str]:
    progressed = False
    for transition in transitions:
        progress = _observation(transition).get("progress") or {}
        progressed = progressed or float(progress.get("level_time", 0.0) or 0.0) > 0.0
        progressed = progressed or int(progress.get("coins_gained", 0) or 0) > 0
        if progressed and _decision(transition).get("action") == "restart":
            return _refs_for(transition)
    return []


def _normal_transitions(transitions: list[Transition]) -> list[str]:
    upgrade = _first_action(transitions, "select_upgrade")
    chest = _first_event(transitions, "chest_collected")
    return upgrade + chest if upgrade and chest else []


def _long_progression(transitions: list[Transition]) -> list[str]:
    for transition in transitions:
        observation = _observation(transition)
        player = observation.get("player") or {}
        progress = observation.get("progress") or {}
        if int(player.get("level", 0) or 0) >= 3 or float(
            progress.get("level_time", 0.0) or 0.0
        ) >= 120.0:
            return _refs_for(transition)
    return []


COVERAGE_REGISTRY: dict[str, CoverageEvaluator] = {
    "observe_player_state": _first_player_state,
    "observe_relative_positions": _first_relative_position,
    "select_upgrade": lambda transitions: _first_action(transitions, "select_upgrade"),
    "collect_chest": lambda transitions: _first_event(transitions, "chest_collected"),
    "reach_multiple_level_ups": _multiple_level_ups,
    "restart_after_progress": _restart_after_progress,
    "observe_valid_state": _first_player_state,
    "complete_normal_transitions": _normal_transitions,
    "sustain_long_progression": _long_progression,
}


def _health_ratio_consistency(transitions: list[Transition]) -> tuple[bool, list[str], str]:
    relevant: list[Transition] = []
    for transition in transitions:
        player = _observation(transition).get("player") or {}
        if not player.get("present"):
            continue
        relevant.append(transition)
        maximum = float(player.get("max_health", 0.0) or 0.0)
        health = float(player.get("health", 0.0) or 0.0)
        ratio = float(player.get("health_ratio", math.nan))
        expected = health / maximum if maximum > 0 else 0.0
        formula_consistent = math.isfinite(ratio) and math.isclose(
            ratio, expected, rel_tol=1e-5, abs_tol=1e-5
        )
        bounded = maximum > 0 and 0.0 <= health <= maximum and 0.0 <= ratio <= 1.0
        game_over_overshoot = (
            _observation(transition).get("phase") == "game_over"
            and player.get("alive") is False
            and maximum > 0
            and health < 0.0
            and ratio < 0.0
        )
        if not formula_consistent or not (bounded or game_over_overshoot):
            return False, _refs_for(transition), "reported health_ratio is inconsistent"
    evidence = _refs_for(relevant[-1]) if relevant else _fallback_refs(transitions)
    return True, evidence, "health values are internally consistent"


def _relative_position_consistency(
    transitions: list[Transition],
) -> tuple[bool, list[str], str]:
    relevant: list[Transition] = []
    for transition in transitions:
        observation = _observation(transition)
        player = observation.get("player") or {}
        entities = (observation.get("world") or {}).get("qa_entities") or []
        if not player.get("present") or not entities:
            continue
        relevant.append(transition)
        position = player.get("position") or {}
        player_x = float(position.get("x", 0.0) or 0.0)
        player_y = float(position.get("y", 0.0) or 0.0)
        for entity in entities:
            expected_x = float(entity.get("x", 0.0) or 0.0) - player_x
            expected_y = float(entity.get("y", 0.0) or 0.0) - player_y
            if not math.isclose(
                float(entity.get("relative_x", math.nan)), expected_x, abs_tol=1e-4
            ) or not math.isclose(
                float(entity.get("relative_y", math.nan)), expected_y, abs_tol=1e-4
            ):
                return False, _refs_for(transition), "reported relative position is inconsistent"
    evidence = _refs_for(relevant[-1]) if relevant else _fallback_refs(transitions)
    return True, evidence, "relative positions are consistent"


def _ability_signature(observation: dict[str, Any]) -> str:
    abilities = (observation.get("inventory") or {}).get("abilities") or []
    return json.dumps(abilities, sort_keys=True, separators=(",", ":"))


def _upgrade_effect(transitions: list[Transition]) -> tuple[bool, list[str], str]:
    for index, transition in enumerate(transitions):
        if _decision(transition).get("action") != "select_upgrade":
            continue
        before = _observation(transitions[index - 1]) if index > 0 else {}
        after = _observation(transition)
        refs = (_refs_for(transitions[index - 1]) if index > 0 else []) + _refs_for(transition)
        changed = bool(before) and _ability_signature(before) != _ability_signature(after)
        return changed, refs or _fallback_refs(transitions), (
            "inventory changed after upgrade selection"
            if changed
            else "upgrade selection was acknowledged without an inventory change"
        )
    return False, _fallback_refs(transitions), "no upgrade selection transition was found"


def _chest_state_transition(transitions: list[Transition]) -> tuple[bool, list[str], str]:
    for index, transition in enumerate(transitions):
        if _event(transition).get("type") != "chest_collected":
            continue
        before = _observation(transitions[index - 1]) if index > 0 else {}
        after = _observation(transition)
        before_count = int(((before.get("world") or {}).get("chest_count") or 0))
        after_count = int(((after.get("world") or {}).get("chest_count") or 0))
        before_progress = before.get("progress") or {}
        after_progress = after.get("progress") or {}
        progress_keys = ("coins_gained", "damage_dealt", "damage_taken")
        progress_changed = any(
            before_progress.get(key) != after_progress.get(key) for key in progress_keys
        )
        changed = before_count != after_count or progress_changed
        refs = (_refs_for(transitions[index - 1]) if index > 0 else []) + _refs_for(
            transition, include_event=True
        )
        return changed, refs, (
            "chest event corresponds to a state transition"
            if changed
            else "chest event has no corresponding state transition"
        )
    return False, _fallback_refs(transitions), "no chest collection transition was found"


def _experience_conservation(transitions: list[Transition]) -> tuple[bool, list[str], str]:
    relevant: list[Transition] = []
    for transition in transitions:
        observation = _observation(transition)
        player = observation.get("player") or {}
        if not player.get("present"):
            continue
        relevant.append(transition)
        experience = float(player.get("exp", 0.0) or 0.0)
        required = float(player.get("next_level_exp", 0.0) or 0.0)
        ratio = float(player.get("exp_ratio", math.nan))
        expected = experience / required if required > 0 else 0.0
        finite = all(math.isfinite(value) for value in (experience, required, ratio))
        ratio_consistent = math.isclose(ratio, expected, rel_tol=1e-5, abs_tol=1e-5)
        if observation.get("phase") == "upgrade_selection":
            valid_range = experience == required and ratio == 1.0
        else:
            valid_range = 0.0 <= experience < required
        if not finite or required <= 0 or not valid_range or not ratio_consistent:
            return False, _refs_for(transition), "experience progression invariant is violated"
    evidence = _refs_for(relevant[-1]) if relevant else _fallback_refs(transitions)
    return True, evidence, "experience progression invariants hold"


def _restart_currency_isolation(transitions: list[Transition]) -> tuple[bool, list[str], str]:
    for index, transition in enumerate(transitions):
        if _decision(transition).get("action") != "restart":
            continue
        after_coins = int(((_observation(transition).get("progress") or {}).get("coins_gained") or 0))
        refs = (_refs_for(transitions[index - 1]) if index > 0 else []) + _refs_for(transition)
        return after_coins == 0, refs, (
            "run currency reset after restart"
            if after_coins == 0
            else "run currency leaked across restart"
        )
    return False, _fallback_refs(transitions), "no restart transition was found"


def _valid_observation(transitions: list[Transition]) -> tuple[bool, list[str], str]:
    health_ok, health_refs, health_detail = _health_ratio_consistency(transitions)
    relative_ok, relative_refs, relative_detail = _relative_position_consistency(transitions)
    return (
        health_ok and relative_ok,
        health_refs + relative_refs,
        f"{health_detail}; {relative_detail}",
    )


def _normal_state_transitions(transitions: list[Transition]) -> tuple[bool, list[str], str]:
    upgrade_ok, upgrade_refs, upgrade_detail = _upgrade_effect(transitions)
    chest_ok, chest_refs, chest_detail = _chest_state_transition(transitions)
    return (
        upgrade_ok and chest_ok,
        upgrade_refs + chest_refs,
        f"{upgrade_detail}; {chest_detail}",
    )


def _stable_long_progression(transitions: list[Transition]) -> tuple[bool, list[str], str]:
    health_ok, health_refs, health_detail = _health_ratio_consistency(transitions)
    experience_ok, experience_refs, experience_detail = _experience_conservation(transitions)
    return (
        health_ok and experience_ok,
        health_refs + experience_refs,
        f"{health_detail}; {experience_detail}",
    )


ORACLE_REGISTRY: dict[str, OracleEvaluator] = {
    "health_ratio_consistency": _health_ratio_consistency,
    "relative_position_consistency": _relative_position_consistency,
    "upgrade_effect": _upgrade_effect,
    "chest_state_transition": _chest_state_transition,
    "experience_conservation": _experience_conservation,
    "restart_currency_isolation": _restart_currency_isolation,
    "valid_observation": _valid_observation,
    "normal_state_transitions": _normal_state_transitions,
    "stable_long_progression": _stable_long_progression,
}


def _v4_refs(transitions: list[Transition]) -> list[str]:
    refs = [
        str((transition.get("observation") or {}).get("observation_id") or "")
        for transition in transitions
    ]
    refs = [reference for reference in refs if reference]
    if not refs:
        raise EvaluationContractError("v4 oracle requires observation evidence")
    return list(dict.fromkeys(refs))


def evaluate_v4_oracle(oracle_id: str, transitions: list[Transition]) -> OracleResult:
    """Evaluate v4 invariants against the additive raw/view observation channels."""

    refs = _v4_refs(transitions)
    if oracle_id == "view_state_match":
        for transition in transitions:
            observation = transition.get("observation") or {}
            player = observation.get("player") or {}
            view = observation.get("player_view") or {}
            if not player.get("present") or not view:
                continue
            maximum = float(player.get("max_health", 0.0) or 0.0)
            raw_health = float(player.get("health", 0.0) or 0.0)
            if maximum > 0 and "health" in view:
                if not math.isclose(float(view["health"]), raw_health, abs_tol=1e-5):
                    return OracleResult(
                        verdict="fail", evidence_refs=refs, detail="displayed health differs from raw health"
                    )
            if maximum > 0 and "health_ratio" in view:
                expected = raw_health / maximum
                if not math.isclose(float(view["health_ratio"]), expected, abs_tol=1e-5):
                    return OracleResult(
                        verdict="fail", evidence_refs=refs, detail="displayed health ratio differs from raw health"
                    )
        return OracleResult(verdict="pass", evidence_refs=refs, detail="raw and displayed health match")

    if oracle_id == "exp_conservation":
        for transition in transitions:
            observation = transition.get("observation") or {}
            player = observation.get("player") or {}
            view = observation.get("player_view") or {}
            if player.get("present") and view and "exp" in view:
                if not math.isclose(float(view["exp"]), float(player.get("exp", 0.0) or 0.0), abs_tol=1e-5):
                    return OracleResult(
                        verdict="fail", evidence_refs=refs, detail="displayed experience differs from raw experience"
                    )
        return OracleResult(verdict="pass", evidence_refs=refs, detail="raw and displayed experience match")

    if oracle_id in {"hp_decreases_on_hit", "item_effect_applied", "item_hit_range"}:
        for index, transition in enumerate(transitions):
            observation = transition.get("observation") or {}
            event_type = str((observation.get("event_state") or {}).get("type") or "")
            if oracle_id == "hp_decreases_on_hit" and event_type in {"player_hit", "hit"}:
                before = (transitions[index - 1].get("observation") or {}).get("player") or {} if index else {}
                after = observation.get("player") or {}
                passed = float(after.get("health", 0.0) or 0.0) < float(before.get("health", 0.0) or 0.0)
                return OracleResult(
                    verdict="pass" if passed else "fail",
                    evidence_refs=refs,
                    detail="player health changed after hit" if passed else "player health did not decrease after hit",
                )
            if oracle_id == "item_effect_applied" and event_type == "item_used":
                before_progress = (transitions[index - 1].get("observation") or {}).get("progress") or {} if index else {}
                after_progress = observation.get("progress") or {}
                passed = float(after_progress.get("damage_dealt", 0.0) or 0.0) > float(before_progress.get("damage_dealt", 0.0) or 0.0)
                return OracleResult(
                    verdict="pass" if passed else "fail",
                    evidence_refs=refs,
                    detail="item damage was observed" if passed else "item use had no damage effect",
                )
            if oracle_id == "item_hit_range" and event_type == "item_used":
                event = observation.get("event_state") or {}
                affected = event.get("targets_affected")
                expected = event.get("targets_in_radius")
                if affected is not None and expected is not None:
                    passed = int(affected) == int(expected)
                    return OracleResult(
                        verdict="pass" if passed else "fail",
                        evidence_refs=refs,
                        detail="item affected every target in range" if passed else "item affected only part of the range",
                    )
        return OracleResult(verdict="not_evaluated", evidence_refs=refs, detail="required transition was not observed")

    raise EvaluationContractError(f"unregistered v4 oracle: {oracle_id}")


def _v4_oracle_tuple(oracle_id: str, transitions: list[Transition]) -> tuple[bool, list[str], str]:
    result = evaluate_v4_oracle(oracle_id, transitions)
    return result.verdict == "pass", result.evidence_refs, result.detail


ORACLE_REGISTRY.update(
    {
        oracle_id: (lambda transitions, oracle_id=oracle_id: _v4_oracle_tuple(oracle_id, transitions))
        for oracle_id in (
            "hp_decreases_on_hit",
            "view_state_match",
            "item_effect_applied",
            "item_hit_range",
            "exp_conservation",
        )
    }
)


def evaluate_coverage(scenario: Scenario, transitions: list[Transition]) -> CoverageResult:
    evaluator = COVERAGE_REGISTRY.get(scenario.coverage_target)
    if evaluator is None:
        raise EvaluationContractError(
            f"unregistered coverage target: {scenario.coverage_target}"
        )
    reached_refs = evaluator(transitions)
    refs = reached_refs or _fallback_refs(transitions)
    _validate_refs(transitions, refs)
    return CoverageResult(
        status="reached" if reached_refs else "not_reached",
        evidence_refs=refs,
        detail=f"coverage target {scenario.coverage_target} "
        f"{'was reached' if reached_refs else 'was not reached'}",
    )


def evaluate_oracle(scenario: Scenario, transitions: list[Transition]) -> OracleResult:
    evaluator = ORACLE_REGISTRY.get(scenario.oracle)
    if evaluator is None:
        raise EvaluationContractError(f"unregistered oracle: {scenario.oracle}")
    coverage = evaluate_coverage(scenario, transitions)
    if coverage.status == "not_reached":
        return OracleResult(
            verdict="not_evaluated",
            evidence_refs=coverage.evidence_refs,
            detail="oracle was not evaluated because coverage was not reached",
        )
    passed, refs, detail = evaluator(transitions)
    _validate_refs(transitions, refs)
    return OracleResult(
        verdict="pass" if passed else "fail",
        evidence_refs=refs,
        detail=detail,
    )
