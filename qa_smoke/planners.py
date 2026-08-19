from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
import time
from typing import Any, Protocol

from .charter import TestCharter
from .memory import SessionMemory


TRACKED_DELTA_PATHS = (
    "scene",
    "player.health",
    "player.max_health",
    "player.health_ratio",
    "player.level",
    "player.exp",
    "player.next_level_exp",
    "player.exp_ratio",
    "player.position.x",
    "player.position.y",
    "progress.level_time",
    "progress.monsters_killed",
    "progress.coins_gained",
    "menu.upgrade_open",
    "menu.game_over",
    "inventory",
    "world.chest_count",
    "world.enemy_count",
    "event_state.type",
    "event_state.event_id",
)


def _nested_value(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for segment in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def compute_observed_delta(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    changes = []
    for path in TRACKED_DELTA_PATHS:
        previous = _nested_value(before, path)
        current = _nested_value(after, path)
        if previous != current:
            changes.append({"path": path, "before": previous, "after": current})
    return {
        "before_observation_id": str(before.get("observation_id") or ""),
        "after_observation_id": str(after.get("observation_id") or ""),
        "changes": changes,
    }


def compact_inventory(inventory: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only inventory counts and owned ability levels for planning context."""
    source = inventory if isinstance(inventory, dict) else {}
    slots = []
    for item in source.get("slots") or []:
        if not isinstance(item, dict):
            continue
        slots.append(
            {
                "index": item.get("index"),
                "type": item.get("type"),
                "count": item.get("count", 0),
                "pending_count": item.get("pending_count", 0),
            }
        )
    abilities = []
    for item in source.get("abilities") or []:
        if not isinstance(item, dict) or not item.get("owned"):
            continue
        abilities.append(
            {
                "name": item.get("name") or item.get("type") or "",
                "level": item.get("level", 0),
            }
        )
    return {"slots": slots, "abilities": abilities}


def compact_observed_delta(
    observed_delta: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compact only the inventory branch while preserving deterministic delta IDs."""
    source = observed_delta if isinstance(observed_delta, dict) else {}
    changes = []
    for change in source.get("changes") or []:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "")
        compacted = {"path": path}
        if path == "inventory":
            compacted["before"] = compact_inventory(change.get("before"))
            compacted["after"] = compact_inventory(change.get("after"))
        else:
            if "before" in change:
                compacted["before"] = change["before"]
            if "after" in change:
                compacted["after"] = change["after"]
        changes.append(compacted)
    return {
        "before_observation_id": str(source.get("before_observation_id") or ""),
        "after_observation_id": str(source.get("after_observation_id") or ""),
        "changes": changes,
    }


def compact_planning_transition(
    transition: dict[str, Any],
    *,
    include_latest_details: bool,
) -> dict[str, Any]:
    """Select the transition fields needed for current or committed planning context."""
    source = transition if isinstance(transition, dict) else {}
    decision = source.get("decision") or source.get("game_action") or {}
    decision = decision if isinstance(decision, dict) else {}
    arguments = decision.get("arguments") or {}
    arguments = arguments if isinstance(arguments, dict) else {}
    action = {
        "tool": str(decision.get("tool") or "game"),
        "action": str(decision.get("action") or ""),
    }
    for key in ("intent", "target_id"):
        if key in arguments:
            action[key] = arguments[key]
    if include_latest_details:
        for key in ("x", "y", "duration", "index"):
            if key in arguments:
                action[key] = arguments[key]

    observed_delta = compact_observed_delta(source.get("observed_delta"))
    observation = source.get("observation") or {}
    observation = observation if isinstance(observation, dict) else {}
    event_state = source.get("event_state") or observation.get("event_state") or {}
    event_state = event_state if isinstance(event_state, dict) else {}
    observation_id = str(
        source.get("observation_id")
        or observation.get("observation_id")
        or observed_delta.get("after_observation_id")
        or ""
    )
    event_id = str(event_state.get("event_id") or "")
    inventory = source.get("inventory") or observation.get("inventory") or {}
    compacted = {
        "step": source.get("step"),
        "observation_id": observation_id,
        "action": action,
        "observed_delta": observed_delta,
        "inventory": compact_inventory(inventory),
        "navigation": dict(source.get("navigation") or {}),
    }
    if event_id:
        compacted["event_id"] = event_id
    if include_latest_details:
        compacted["expected_effect"] = str(decision.get("expected_effect") or "")
        compacted["reflection"] = dict(decision.get("reflection") or {})
        if action["tool"] in ("source_search", "source_read") and "result" in source:
            compacted["source_tool_result"] = source["result"]
    return compacted


def observation_phase(observation: dict[str, Any]) -> str:
    """Return an explicit gameplay phase, including compatibility with protocol 1.2."""
    phase = str(observation.get("phase") or "").strip().lower()
    if phase:
        return phase
    actions = set(observation.get("available_actions") or [])
    menu = observation.get("menu") or {}
    player = observation.get("player") or {}
    if "start_game" in actions:
        return "character_select"
    if menu.get("upgrade_open") or "select_upgrade" in actions:
        return "upgrade_selection"
    if menu.get("game_over") or (player.get("present") and not player.get("alive", True)):
        return "game_over"
    if player.get("present") and player.get("alive", True):
        return "active_gameplay"
    return "loading_or_unavailable"


def observation_pause_reason(observation: dict[str, Any]) -> str:
    reason = str(observation.get("pause_reason") or "").strip().lower()
    if reason:
        return reason
    if not observation.get("paused"):
        return "simulation_running"
    phase = observation_phase(observation)
    if phase == "character_select":
        return "character_select"
    if phase == "upgrade_selection":
        return "upgrade_dialog"
    if phase == "game_over":
        return "game_over"
    if (observation.get("event_state") or {}).get("type"):
        return "event_decision_boundary"
    return "agent_decision_boundary"


def _has_debug_signal(observation: dict[str, Any]) -> bool:
    if observation.get("ok") is False:
        return True
    for item in observation.get("recent_logs") or []:
        lowered = str(item).lower()
        if any(marker in lowered for marker in ("exception", "error:", "assert:")):
            return True
    return False


def build_action_contract(
    observation: dict[str, Any],
    mode: str,
    charter: TestCharter | None = None,
    source_steps_remaining: int = 0,
) -> dict[str, Any]:
    """Describe which call advances the current phase without choosing its gameplay vector."""
    phase = observation_phase(observation)
    actions = set(observation.get("available_actions") or [])
    allowed_calls: list[str]
    reason: str
    required_arguments: dict[str, Any]
    allowed_indices: list[int] = []
    if phase == "character_select":
        allowed_calls = ["game.start_game"]
        character_count = int(((observation.get("menu") or {}).get("character_count") or 0))
        allowed_indices = list(range(max(0, character_count)))
        required_arguments = {
            "index": f"integer character index{f' from {allowed_indices}' if allowed_indices else ''}"
        }
        reason = "Choose a character once. start_game is not an unpause command."
    elif phase == "upgrade_selection":
        allowed_calls = ["game.select_upgrade"]
        allowed_indices = [
            int(choice.get("index"))
            for choice in ((observation.get("menu") or {}).get("choices") or [])
            if isinstance(choice, dict) and choice.get("index") is not None
        ]
        required_arguments = {
            "index": f"integer chosen from available_upgrade_indices={allowed_indices}"
        }
        reason = "Resolve the blocking level-up choice."
    elif phase == "game_over":
        allowed_calls = [
            f"game.{action}" for action in ("restart", "return_to_menu") if action in actions
        ] or ["game.observe"]
        required_arguments = {}
        reason = "Resolve the terminal run state."
    elif phase == "active_gameplay":
        allowed_calls = ["game.direct_steer"] if "direct_steer" in actions else []
        if not allowed_calls:
            allowed_calls = [f"game.{action}" for action in ("move", "steer") if action in actions]
        required_arguments = {
            "x": "finite number selected by the LLM",
            "y": "finite number selected by the LLM",
            "duration": "positive number",
            "intent": "short string",
            "target_id": "integer chest ID, or 0 when not targeting a chest",
        }
        reason = (
            "The game is already started. Supply the next movement vector; do not call start_game, "
            "wait, observe, or pause merely because the bridge sampled at a paused decision boundary."
        )
    else:
        useful = [action for action in actions if action not in ("pause", "shutdown")]
        allowed_calls = [f"game.{action}" for action in useful] or ["game.observe"]
        required_arguments = {}
        reason = "Use an action exposed by the bridge for the current phase."

    source_allowed = (
        mode == "qa"
        and source_steps_remaining > 0
        and _has_debug_signal(observation)
        and phase not in ("character_select", "upgrade_selection")
    )
    if source_allowed:
        allowed_calls.extend(("source_search", "source_read"))
    return {
        "phase": phase,
        "allowed_calls": allowed_calls,
        "source_steps_remaining": source_steps_remaining,
        "source_tools_allowed": source_allowed,
        "required_arguments": required_arguments,
        "allowed_indices": allowed_indices,
        "requires_nonzero_movement": bool(
            phase == "active_gameplay"
            and charter is not None
            and charter.movement_constraint != "free"
        ),
        "reason": reason,
    }


def build_reflection_contract(
    previous_transition: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build reflection rules and evidence IDs from one sanitized transition."""
    has_previous_transition = bool(previous_transition)
    allowed_evidence_refs: list[str] = []
    if isinstance(previous_transition, dict):
        observation = previous_transition.get("observation")
        if not isinstance(observation, dict):
            observation = previous_transition
        observation_id = str(observation.get("observation_id") or "").strip()
        if observation_id:
            allowed_evidence_refs.append(observation_id)
        event = observation.get("event_state")
        if not isinstance(event, dict):
            event = previous_transition.get("event_state") or {}
        event_id = str(event.get("event_id") or "").strip()
        if event_id and event_id not in allowed_evidence_refs:
            allowed_evidence_refs.append(event_id)

    return {
        "has_previous_transition": has_previous_transition,
        "allowed_statuses": (
            ["matched", "unexpected", "uncertain"]
            if has_previous_transition
            else ["not_applicable"]
        ),
        "evidence_refs_required": has_previous_transition,
        "candidate_id_required_for": ["unexpected"],
        "allowed_evidence_refs": allowed_evidence_refs,
    }


def canonicalize_decision_arguments(decision: dict[str, Any]) -> dict[str, Any]:
    """Normalize only the model's argument syntax; never invent a gameplay choice."""
    canonical = dict(decision)
    arguments = canonical.get("arguments")
    normalizations: list[str] = []
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
            normalizations.append("decoded_json_string")
        except json.JSONDecodeError:
            pass
    if isinstance(arguments, list) and len(arguments) == 1:
        arguments = arguments[0]
        normalizations.append("unwrapped_single_item_array")

    action = str(canonical.get("action") or "").strip().lower()
    if action in ("start_game", "select_upgrade", "use_item"):
        if isinstance(arguments, (int, float)) and not isinstance(arguments, bool):
            arguments = {"index": int(arguments)}
            normalizations.append("mapped_numeric_argument_to_index")
        elif isinstance(arguments, dict) and "index" not in arguments:
            for alias in ("choice_index", "upgrade_index", "character_index", "slot_index"):
                if alias in arguments:
                    arguments = {**arguments, "index": arguments[alias]}
                    normalizations.append(f"mapped_{alias}_to_index")
                    break

    canonical["arguments"] = arguments
    if normalizations:
        canonical["_syntax_normalizations"] = normalizations
    return canonical


def build_decision_response_schema(contract: dict[str, Any]) -> dict[str, Any] | None:
    """Build a strict schema when the phase has exactly one unambiguous game-call shape."""
    allowed_calls = contract.get("allowed_calls") or []
    if len(allowed_calls) != 1 or not str(allowed_calls[0]).startswith("game."):
        return None
    action = str(allowed_calls[0]).split(".", 1)[1]
    reflection_contract = contract.get("reflection_contract") or {}
    allowed_reflection_statuses = reflection_contract.get("allowed_statuses")
    if not isinstance(allowed_reflection_statuses, list) or not allowed_reflection_statuses:
        allowed_reflection_statuses = (
            ["matched", "unexpected", "uncertain"]
            if contract.get("has_previous_transition")
            else ["not_applicable"]
        )
    argument_properties: dict[str, Any] = {}
    required_arguments: list[str] = []
    if action in ("start_game", "select_upgrade", "use_item"):
        index_schema: dict[str, Any] = {"type": "integer"}
        allowed_indices = contract.get("allowed_indices") or []
        if allowed_indices:
            index_schema["enum"] = list(allowed_indices)
        argument_properties = {"index": index_schema}
        required_arguments = ["index"]
    elif action in ("direct_steer", "steer", "move"):
        argument_properties = {
            "x": {"type": "number"},
            "y": {"type": "number"},
            "duration": {"type": "number"},
        }
        required_arguments = ["x", "y", "duration"]
        if action == "direct_steer":
            argument_properties.update(
                {
                    "intent": {"type": "string"},
                    "target_id": {"type": "integer"},
                }
            )
            required_arguments.extend(("intent", "target_id"))
    reflection_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": allowed_reflection_statuses,
            },
            "summary": {"type": "string"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "candidate_id": {"type": "string"},
            "reproduction_attempted": {"type": "boolean"},
        },
        "required": [
            "status",
            "summary",
            "evidence_refs",
            "candidate_id",
            "reproduction_attempted",
        ],
        "additionalProperties": False,
    }
    required_fields = [
        "plan",
        "hypothesis",
        "qa_observation",
        "tool",
        "action",
        "arguments",
        "reflection",
    ]
    requires_expected = (
        not contract.get("has_previous_transition")
        or action
        in (
            "direct_steer",
            "steer",
            "move",
            "wait",
            "select_upgrade",
            "use_item",
            "restart",
            "start_game",
            "return_to_menu",
        )
    )
    if requires_expected:
        required_fields.append("expected_effect")
    return {
        "type": "object",
        "properties": {
            "plan": {"type": "string"},
            "hypothesis": {"type": "string"},
            "qa_observation": {"type": "string"},
            "tool": {"type": "string", "enum": ["game"]},
            "action": {"type": "string", "enum": [action]},
            "arguments": {
                "type": "object",
                "properties": argument_properties,
                "required": required_arguments,
                "additionalProperties": False,
            },
            "expected_effect": {"type": "string"},
            "reflection": reflection_schema,
        },
        "required": required_fields,
        "additionalProperties": False,
    }


def validate_decision_against_contract(
    decision: dict[str, Any], contract: dict[str, Any]
) -> str | None:
    tool = str(decision.get("tool") or "").strip().lower()
    action = str(decision.get("action") or "").strip().lower()
    call = f"game.{action}" if tool == "game" else tool
    allowed = contract.get("allowed_calls") or []
    if call not in allowed:
        return f"{call or '<missing tool>'} is invalid in phase {contract.get('phase')}; allowed: {allowed}"
    arguments = decision.get("arguments")
    if not isinstance(arguments, dict):
        return (
            "arguments must be a JSON object matching "
            f"{contract.get('required_arguments') or {}}; use an empty object only when no arguments are required"
        )
    if call == "game.direct_steer":
        try:
            x = float(arguments["x"])
            y = float(arguments["y"])
            duration = float(arguments["duration"])
        except (KeyError, TypeError, ValueError):
            return "direct_steer requires numeric x, y, and duration"
        if not all(math.isfinite(value) for value in (x, y, duration)):
            return "direct_steer x, y, and duration must be finite"
        if duration <= 0:
            return "direct_steer duration must be positive"
        if contract.get("requires_nonzero_movement") and math.hypot(x, y) < 0.05:
            return "the requested net-progress charter requires a non-zero direct_steer vector"
    if call == "source_search" and not str(arguments.get("query") or "").strip():
        return "source_search requires a non-empty query"
    if call == "source_read" and not str(arguments.get("path") or "").strip():
        return "source_read requires a path"
    if call in ("game.start_game", "game.select_upgrade", "game.use_item"):
        try:
            index = int(arguments["index"])
        except (KeyError, TypeError, ValueError):
            return f"{call} requires an integer arguments.index"
        allowed_indices = contract.get("allowed_indices") or []
        if allowed_indices and index not in allowed_indices:
            return f"arguments.index={index} is not one of the available indices {allowed_indices}"
    if not isinstance(decision.get("qa_observation"), str):
        return "qa_observation must be a string separate from the navigation hypothesis"
    reflection = decision.get("reflection")
    if not isinstance(reflection, dict):
        return "reflection must be a JSON object"
    reflection_status = str(reflection.get("status") or "")
    reflection_contract = contract.get("reflection_contract") or {}
    allowed_reflection_statuses = set(
        reflection_contract.get("allowed_statuses") or (
            ["matched", "unexpected", "uncertain"]
            if contract.get("has_previous_transition")
            else ["not_applicable"]
        )
    )
    if reflection_status not in allowed_reflection_statuses:
        return f"reflection.status must be one of {sorted(allowed_reflection_statuses)}"
    has_previous = bool(
        reflection_contract.get(
            "has_previous_transition", contract.get("has_previous_transition")
        )
    )
    if has_previous and reflection_status == "not_applicable":
        return "reflection.status cannot be not_applicable when a previous transition exists"
    if not has_previous and reflection_status != "not_applicable":
        return "the first planning step requires reflection.status=not_applicable"
    if not isinstance(reflection.get("summary"), str):
        return "reflection.summary must be a string"
    evidence_refs = reflection.get("evidence_refs")
    if not isinstance(evidence_refs, list) or any(
        not isinstance(reference, str) for reference in evidence_refs
    ):
        return "reflection.evidence_refs must be an array of strings"
    if has_previous and reflection_status != "not_applicable" and not evidence_refs:
        return "reflection must cite evidence_refs from the observed transition"
    allowed_evidence_refs = reflection_contract.get("allowed_evidence_refs")
    if (
        has_previous
        and isinstance(allowed_evidence_refs, list)
        and allowed_evidence_refs
        and any(reference not in allowed_evidence_refs for reference in evidence_refs)
    ):
        return (
            "reflection.evidence_refs must use allowed transition IDs: "
            f"{allowed_evidence_refs}"
        )
    if reflection_status == "unexpected" and not str(
        reflection.get("candidate_id") or ""
    ).strip():
        return "unexpected reflection requires candidate_id"
    if not isinstance(reflection.get("reproduction_attempted"), bool):
        return "reflection.reproduction_attempted must be a boolean"
    expected_required = (
        tool not in ("source_search", "source_read")
        and (
            not has_previous
            or call
            in {
                "game.direct_steer",
                "game.steer",
                "game.move",
                "game.wait",
                "game.select_upgrade",
                "game.use_item",
                "game.restart",
                "game.start_game",
                "game.return_to_menu",
            }
        )
    )
    if expected_required and not str(decision.get("expected_effect") or "").strip():
        return f"{call} requires a non-empty expected_effect before execution"
    return None


def compact_observation(observation: dict[str, Any], max_threats: int = 8, max_chests: int = 8) -> dict[str, Any]:
    """Keep raw artifacts intact while sending only decision-relevant state to the LLM."""
    world = observation.get("world") or {}
    entities = sorted(
        (item for item in (world.get("qa_entities") or []) if isinstance(item, dict)),
        key=lambda item: float(item.get("distance", float("inf")) or float("inf")),
    )[:max_threats]
    chests = sorted(
        (item for item in (world.get("visible_chests") or []) if isinstance(item, dict)),
        key=lambda item: float(item.get("distance", float("inf")) or float("inf")),
    )[:max_chests]
    compact_world = {
        key: world.get(key)
        for key in (
            "enemy_count", "pickup_count", "chest_count", "nearest_enemy_distance",
            "nearest_enemy_vector", "nearest_chest_distance", "nearest_chest_vector",
            "nearest_pickup_distance", "nearest_pickup_vector", "nearest_pickup_kind",
            "nearby_enemy_count", "danger_score", "escape_vector", "enemy_octants",
            "mini_boss_spawned", "final_boss_spawned",
        )
    }
    compact_world["threat_entities"] = entities
    compact_world["visible_chests"] = chests

    logs: list[str] = []
    for item in reversed(observation.get("recent_logs") or []):
        text = str(item)
        lowered = text.lower()
        if text not in logs and any(marker in lowered for marker in ("exception", "error:", "assert:", "warning:")):
            logs.append(text)
        if len(logs) >= 4:
            break
    logs.reverse()
    pause_reason = observation_pause_reason(observation)
    return {
        "phase": observation_phase(observation),
        "scene": observation.get("scene"),
        "bridge_clock": {
            "paused_at_observation": bool(observation.get("paused")),
            "time_scale_at_observation": observation.get("time_scale"),
            "pause_reason": pause_reason,
            "agent_decision_boundary": pause_reason in (
                "agent_decision_boundary", "event_decision_boundary"
            ),
        },
        "player": observation.get("player") or {},
        "world": compact_world,
        "progress": observation.get("progress") or {},
        "menu": observation.get("menu") or {},
        "inventory": compact_inventory(observation.get("inventory")),
        "controller": observation.get("controller") or {},
        "event_state": observation.get("event_state") or {},
        "available_actions": observation.get("available_actions") or [],
        "recent_logs": logs,
    }


class Planner(Protocol):
    def plan(
        self,
        observation: dict[str, Any],
        step: int,
        tool_context: list[dict[str, Any]],
        source_steps_remaining: int = 0,
    ) -> dict[str, Any]: ...

    def final_assessment(self, context: dict[str, Any]) -> dict[str, Any] | None: ...

    def take_last_usage(self) -> dict[str, int]: ...


class HeuristicPlanner:
    """Deterministic policy used to verify the full runtime without an API key."""

    def __init__(self, plan_horizon_seconds: float = 5.0, charter: TestCharter | None = None) -> None:
        self.plan_horizon_seconds = plan_horizon_seconds
        self.charter = charter or TestCharter()
        self.restart_count = 0

    def plan(
        self,
        observation: dict[str, Any],
        step: int,
        tool_context: list[dict[str, Any]],
        source_steps_remaining: int = 0,
    ) -> dict[str, Any]:
        actions = observation.get("available_actions") or []
        menu = observation.get("menu") or {}
        inventory = observation.get("inventory") or {}
        world = observation.get("world") or {}

        if "start_game" in actions:
            return self._game("Enter gameplay with the first unlocked character.", "start_game", index=0)
        if menu.get("upgrade_open") and "select_upgrade" in actions:
            return self._game("Resolve the blocking level-up dialog.", "select_upgrade", index=0)
        if menu.get("game_over") and "restart" in actions and self.restart_count < self.charter.max_restarts:
            self.restart_count += 1
            return self._game("Verify that a dead run can restart as an independent session.", "restart")

        for slot in inventory.get("slots") or []:
            if slot.get("count", 0) > 0 and step % 11 == 0 and "use_item" in actions:
                return self._game("Exercise an available inventory item.", "use_item", index=slot["index"])

        if "steer" in actions or "move" in actions:
            constrained_vector = self.charter.movement_vector
            if constrained_vector is not None:
                dx, dy = constrained_vector
                plan = (
                    f"Maintain {self.charter.movement_constraint} progress while the local controller "
                    "avoids threats and opportunistically collects nearby chests."
                )
            else:
                nearest = world.get("nearest_enemy_vector") or {}
                dx = -float(nearest.get("x", 0.0))
                dy = -float(nearest.get("y", 0.0))
                magnitude = math.hypot(dx, dy)
                if magnitude < 0.01:
                    angle = (step % 12) * math.pi / 6.0
                    dx, dy = math.cos(angle), math.sin(angle)
                else:
                    dx, dy = dx / magnitude, dy / magnitude
                plan = "Choose a safe exploration heading; local steering handles threats and nearby chests every frame."
            action = "steer" if "steer" in actions else "move"
            return self._game(plan, action, x=round(dx, 4), y=round(dy, 4), duration=self.plan_horizon_seconds)
        if "wait" in actions:
            return self._game("Advance a bounded simulation interval.", "wait", duration=self.plan_horizon_seconds)
        return self._game("Refresh state when no progress action is available.", "observe")

    def final_assessment(self, context: dict[str, Any]) -> dict[str, Any] | None:
        return None

    @staticmethod
    def take_last_usage() -> dict[str, int]:
        return {}

    @staticmethod
    def _game(plan: str, name: str, **arguments: Any) -> dict[str, Any]:
        return {"plan": plan, "hypothesis": "", "tool": "game", "action": name, "arguments": arguments}


class LLMPlanner:
    def __init__(
        self,
        mode: str,
        model: str,
        charter: TestCharter,
        plan_horizon_seconds: float,
        api_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.mode = mode
        self.model = model
        self.charter = charter
        self.plan_horizon_seconds = plan_horizon_seconds
        self.api_url = api_url or os.environ.get("QA_API_URL", "https://api.openai.com/v1/chat/completions")
        self.api_key = api_key or os.environ.get("QA_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        if not self.model:
            raise ValueError("LLM policy requires --model or QA_MODEL")
        if not self.api_key:
            raise ValueError("LLM policy requires QA_API_KEY or OPENAI_API_KEY")
        self.last_usage: dict[str, int] = {}

    def plan(
        self,
        observation: dict[str, Any],
        step: int,
        tool_context: list[dict[str, Any]],
        source_steps_remaining: int = 0,
    ) -> dict[str, Any]:
        system = self._planning_system_prompt()
        payload = self._planning_payload(observation, step, tool_context, source_steps_remaining)
        schema = build_decision_response_schema(payload["action_contract"])
        return self._request(
            system, json.dumps(payload, ensure_ascii=False), max_tokens=300, response_schema=schema
        )

    def repair_plan(
        self,
        observation: dict[str, Any],
        step: int,
        tool_context: list[dict[str, Any]],
        invalid_decision: dict[str, Any],
        contract_error: str,
        source_steps_remaining: int = 0,
    ) -> dict[str, Any]:
        system = self._planning_system_prompt() + (
            "\nYour previous response violated the current action contract. Correct it once. "
            "Do not repeat or explain the invalid call; return only a valid JSON decision. "
            "Preserve valid gameplay tool, action, and arguments. Correct only the fields named by contract_error. "
            "Do not invent evidence IDs or candidate IDs."
        )
        payload = self._planning_payload(observation, step, tool_context, source_steps_remaining)
        payload["invalid_decision"] = invalid_decision
        payload["contract_error"] = contract_error
        schema = build_decision_response_schema(payload["action_contract"])
        return self._request(
            system, json.dumps(payload, ensure_ascii=False), max_tokens=300, response_schema=schema
        )

    def _planning_system_prompt(self) -> str:
        mode_note = (
            "You are in QA mode. Source tools may be used only when the current action_contract explicitly allows them."
            if self.mode == "qa"
            else "You are in Player Exploring mode. Use only player-perceptible state and game actions."
        )
        return f"""You are an autonomous gameplay QA agent for a Vampire Survivors-style Unity game.
{mode_note}
The bridge_clock is a synchronization state, not a menu command. paused_at_observation=true with pause_reason=agent_decision_boundary means the game is already started and waiting for your next action. Never use start_game or wait to "unpause" it. Use observation.phase and action_contract as authoritative.
During normal direct control, Unity may keep holding your previous vector while this API call is pending. Choose exactly one bounded next tool call promptly.
Game actions: observe, start_game(index), direct_steer(x,y,duration,intent,target_id), wait(duration), select_upgrade(index), use_item(index), restart, return_to_menu.
QA-only tools: source_search(query), source_read(path,line_start,line_count).
The response MUST use one of action_contract.allowed_calls and arguments MUST always be a JSON object matching action_contract.required_arguments. For actions without arguments, return an empty object {{}}. Prefer progress and coverage, react immediately to upgrade dialogs, and never invent an unavailable game action.
Follow the supplied test charter. A named heading is a long-term NET-PROGRESS goal, not a per-action axis lock. Lateral detours and temporary backtracking are allowed for survival and chest collection. The executor never silently corrects your vector.
During active gameplay use direct_steer. You alone must decide whether to continue the requested heading, evade enemies, or approach a specific chest from world.visible_chests. Set target_id to that chest ID when collecting; otherwise use 0. Set intent to a short value such as explore, evade, collect_chest, reposition, or hold.
Unity does NOT automatically avoid enemies, choose a chest, attract toward a chest, enforce the requested heading, or alter your direction. It only holds your chosen vector every frame and detects events. Use world.threat_entities, danger_score, escape_vector, and chest relative vectors to choose x/y yourself.
When intent=collect_chest, aim x/y toward that target's relative_x/relative_y (normally the normalized target vector); do not claim collection while moving away from it. When danger is high, an evade vector should materially align with escape_vector. Choose full 2D movement, not only a cardinal axis.
The horizon can end early on a chest entering close-control range, chest collection, low health, danger spikes, stuck detection, level-up, death, or another event. Re-plan from event_state and controller state.
Act as a QA engineer while you play. Before every state-changing game action, state a concrete expected_effect. On the next planning step, compare the compact prior outcome with that expectation in reflection. Use action_contract.has_previous_transition, not the numeric step, to decide reflection.status. When it is false, use not_applicable with empty evidence_refs and candidate_id. When it is true, never use not_applicable. Return evidence_refs as an empty array; the runner deterministically inserts the latest allowed transition IDs before validation. uncertain may describe an unresolved risk and may leave candidate_id empty. unexpected always requires a stable non-empty candidate_id.
Keep navigation reasoning in hypothesis and QA findings in qa_observation. An unexpected result starts a hypothesis; mark reproduction_attempted only when you deliberately repeated a relevant setup/action. A candidate becomes confirmed only after its own reproduction path, never from hidden evaluator data.
For direct_steer and wait, normally request a duration no greater than {self.plan_horizon_seconds:.3f} simulation seconds.
Return one JSON object only with keys: plan, hypothesis, qa_observation, tool, action, arguments, expected_effect, reflection.
Keep plan, hypothesis, qa_observation, expected_effect, and reflection.summary concise. tool must be game, source_search, or source_read. Player mode must always use game."""

    def _planning_payload(
        self,
        observation: dict[str, Any],
        step: int,
        tool_context: list[dict[str, Any]],
        source_steps_remaining: int,
    ) -> dict[str, Any]:
        memory = SessionMemory.from_transitions(tool_context)
        recent_transitions = memory.recent_transitions()
        action_contract = build_action_contract(
            observation, self.mode, self.charter, source_steps_remaining
        )
        previous_transition = recent_transitions[-1] if recent_transitions else None
        reflection_contract = build_reflection_contract(previous_transition)
        action_contract["has_previous_transition"] = (
            reflection_contract["has_previous_transition"]
        )
        action_contract["reflection_contract"] = reflection_contract
        return {
            "step": step,
            "test_charter": self.charter.as_dict(),
            "observation": compact_observation(observation),
            "action_contract": action_contract,
            "reflection_contract": reflection_contract,
            "previous_transition": previous_transition,
            "recent_transitions": recent_transitions,
            "session_memory": memory.summary(),
            "observed_delta": (
                previous_transition.get("observed_delta")
                if isinstance(previous_transition, dict)
                else None
            ),
        }

    def final_assessment(self, context: dict[str, Any]) -> dict[str, Any] | None:
        system = """You are a senior game QA engineer. Report only the agent's confirmed hypotheses from its own reproduction attempts. Return a JSON object with keys executive_summary, bug_candidates, coverage_gaps. Do not infer bugs from evaluator data, hidden fault identities, rule-based anomaly output, or normal gameplay outcomes."""
        agent_context = {
            "mode": context.get("mode"),
            "policy": context.get("policy"),
            "test_charter": context.get("test_charter"),
            "charter_compliance": context.get("charter_compliance"),
            "metrics": context.get("metrics"),
            "confirmed_hypotheses": context.get("confirmed_hypotheses") or [],
            "fatal_error": context.get("fatal_error"),
        }
        return self._request(
            system, json.dumps(agent_context, ensure_ascii=False), max_tokens=1400
        )

    def _request(
        self,
        system: str,
        user: str,
        max_tokens: int = 300,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": self._response_format(response_schema),
            "max_tokens": max_tokens,
        }
        if self.api_url.startswith("https://api.openai.com/"):
            body["prompt_cache_key"] = f"vsc-gameplay-qa-{self.mode}-{self.model}-v2"
        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        request_started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                response_body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM API HTTP {error.code}: {detail[:1000]}") from error
        self.last_usage = self._normalize_usage(response_body.get("usage") or {})
        self.last_usage["latency_ms"] = max(0, round((time.monotonic() - request_started) * 1000))
        self.last_usage["request_count"] = 1
        choice = response_body["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError("LLM response was truncated at the output token limit")
        content = choice["message"]["content"]
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
        return self._parse_json(str(content))

    def _response_format(self, response_schema: dict[str, Any] | None) -> dict[str, Any]:
        if (
            response_schema is not None
            and self.api_url.startswith("https://api.openai.com/")
            and self.model.startswith("gpt-4o-mini")
        ):
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": "gameplay_qa_decision",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        return {"type": "json_object"}

    def take_last_usage(self) -> dict[str, int]:
        usage = self.last_usage
        self.last_usage = {}
        return usage

    @staticmethod
    def _normalize_usage(usage: dict[str, Any]) -> dict[str, int]:
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        aliases = {
            "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
            "total_tokens": usage.get("total_tokens", 0),
            "cached_tokens": prompt_details.get("cached_tokens", usage.get("cached_tokens", 0)),
            "cache_write_tokens": prompt_details.get("cache_write_tokens", usage.get("cache_write_tokens", 0)),
            "reasoning_tokens": completion_details.get("reasoning_tokens", usage.get("reasoning_tokens", 0)),
        }
        normalized: dict[str, int] = {}
        for key, value in aliases.items():
            try:
                normalized[key] = max(0, int(value or 0))
            except (TypeError, ValueError):
                normalized[key] = 0
        if normalized["total_tokens"] == 0:
            normalized["total_tokens"] = normalized["prompt_tokens"] + normalized["completion_tokens"]
        return normalized

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            raise ValueError("LLM response must be a JSON object")
        return parsed
