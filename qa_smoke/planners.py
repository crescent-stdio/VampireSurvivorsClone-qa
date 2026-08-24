from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
import urllib.error
import urllib.request
import time
from typing import Any, Protocol

from .charter import TestCharter
from .memory import PlanningHistory, sanitize_agent_channel
from .state_channels import project_public_player_view


DEFAULT_LLM_API_URL = "https://api.openai.com/v1/chat/completions"


def resolve_llm_api_url(api_url: str | None) -> str:
    """Match the planner's explicit, environment, then default URL precedence."""

    return api_url or os.environ.get("QA_API_URL", DEFAULT_LLM_API_URL)


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
        if not observation_id:
            observed_delta = previous_transition.get("observed_delta") or {}
            if isinstance(observed_delta, dict):
                fallback_id = str(
                    observed_delta.get("after_observation_id") or ""
                ).strip()
                if fallback_id and fallback_id not in allowed_evidence_refs:
                    allowed_evidence_refs.insert(0, fallback_id)

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


def inject_reflection_evidence_refs(
    decision: dict[str, Any],
    reflection_contract: dict[str, Any],
) -> dict[str, Any]:
    """Replace model-selected evidence with the deterministic latest transition IDs."""
    injected = dict(decision)
    reflection = decision.get("reflection")
    if not isinstance(reflection, dict):
        return injected
    injected_reflection = dict(reflection)
    injected_reflection["evidence_refs"] = list(
        reflection_contract.get("allowed_evidence_refs") or []
    )
    injected["reflection"] = injected_reflection
    return injected


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
        if sanitize_agent_channel(text) is None:
            continue
        lowered = text.lower()
        if text not in logs and any(marker in lowered for marker in ("exception", "error:", "assert:", "warning:")):
            logs.append(text)
        if len(logs) >= 4:
            break
    logs.reverse()
    pause_reason = observation_pause_reason(observation)
    compacted = {
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
    player_view = project_public_player_view(observation.get("player_view"))
    if player_view:
        compacted["player_view"] = player_view
    return compacted


# Statuses worth another attempt. 400/401/403/404/413/422 are deliberately
# absent: they are deterministic misconfigurations, and retrying one only turns
# a fast failure into a slow one while burning quota.
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_WAIT_SECONDS = 20.0
# Measured completion tokens per planning call across the recorded runs sit at
# 177-269, i.e. the mean already reaches 59-90% of the old 300 cap, so the upper
# tail crossed it several times per run. max_tokens is a ceiling rather than a
# reservation, so responses that already fit cost exactly what they did before.
PLANNING_MAX_TOKENS = 700
# Reasoning models spend part of the completion budget on hidden reasoning before
# emitting anything, so a planning-sized cap truncates every call. They also reject
# max_tokens outright in favour of max_completion_tokens.
REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")
REASONING_OUTPUT_FLOOR = 8000
REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})


def is_reasoning_model(model: str) -> bool:
    return any(str(model).startswith(prefix) for prefix in REASONING_MODEL_PREFIXES)
# Matches the final-assessment budget, so no new magic number enters the file.
TRUNCATION_RETRY_MAX_TOKENS = 1400
_DURATION_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|s|m|h)")
_BODY_RETRY_PATTERN = re.compile(
    r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|s)", re.IGNORECASE
)
_DURATION_SCALE = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _parse_duration(text: str) -> float | None:
    """Parse an OpenAI-style duration such as "11.169s", "250ms" or "1m30s"."""
    matches = _DURATION_PATTERN.findall(text.strip())
    if not matches:
        return None
    return sum(float(value) * _DURATION_SCALE[unit] for value, unit in matches)


def parse_retry_after(headers: Any, body: str) -> float | None:
    """Recover the provider's own "wait this long" hint.

    Checked in descending order of authority. The body regex is last but is not
    a fallback of last resort in practice: every rate-limited run this harness
    has recorded carried the delay only in the message text.
    """
    getter = getattr(headers, "get", None)
    if callable(getter):
        raw = getter("Retry-After")
        if raw:
            try:
                return max(0.0, float(str(raw).strip()))
            except ValueError:
                try:
                    from email.utils import parsedate_to_datetime

                    target = parsedate_to_datetime(str(raw))
                    return max(0.0, (target - datetime.now(target.tzinfo)).total_seconds())
                except (TypeError, ValueError):
                    pass
        for name in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
            raw = getter(name)
            if raw:
                parsed = _parse_duration(str(raw))
                if parsed is not None:
                    return parsed
    match = _BODY_RETRY_PATTERN.search(body or "")
    if match:
        return float(match.group(1)) * _DURATION_SCALE[match.group(2).lower()]
    return None


def _is_retryable(error: Exception) -> bool:
    return bool(getattr(error, "retryable", False))


def _transport_error(
    message: str, *, retryable: bool, retry_after: float | None = None
) -> "LLMTransportError":
    error = LLMTransportError(message)
    error.retryable = retryable
    error.retry_after = retry_after
    return error


def _response_error(message: str, *, retryable: bool) -> "LLMResponseError":
    error = LLMResponseError(message)
    error.retryable = retryable
    error.retry_after = None
    return error


class LLMTransportError(RuntimeError):
    """The request never produced a usable HTTP response.

    Note the name: run.build_session_verdict classifies a fatal error by the
    prefix of its class name, and only BridgeContractError/ScenarioContractError/
    LLMContractError map to contract_error. Transport failures are the provider's
    or the network's, never the agent's, so they must keep landing in
    infrastructure_error.
    """


class LLMResponseError(RuntimeError):
    """The HTTP response arrived but was not a usable completion envelope."""


class LLMTruncationError(RuntimeError):
    """The model hit the output ceiling twice and never finished its JSON."""


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
        *,
        max_attempts: int = 3,
        retry_budget_seconds: float = 45.0,
        reasoning_effort: str | None = None,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
        urlopen: Any = None,
    ) -> None:
        self.mode = mode
        self.model = model
        self.charter = charter
        self.plan_horizon_seconds = plan_horizon_seconds
        self.api_url = resolve_llm_api_url(api_url)
        self.api_key = api_key or os.environ.get("QA_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        if not self.model:
            raise ValueError("LLM policy requires --model or QA_MODEL")
        if not self.api_key:
            raise ValueError("LLM policy requires QA_API_KEY or OPENAI_API_KEY")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if retry_budget_seconds < 0:
            raise ValueError("retry_budget_seconds must be zero or greater")
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"unsupported reasoning_effort: {reasoning_effort}")
        self.reasoning_effort = reasoning_effort
        self.max_attempts = max_attempts
        self.retry_budget_seconds = retry_budget_seconds
        self.sleep = sleep
        self.monotonic = monotonic
        self._urlopen = urlopen or urllib.request.urlopen
        self.last_usage: dict[str, int] = {}
        self._last_model_content: str | None = None
        self._retry_count = 0
        self._retry_wait_seconds = 0.0
        self._http_attempts = 0
        self._completion_requests = 0
        self._planning_history = PlanningHistory()
        self._pending_user_content: str | None = None
        self._pending_tool_context: list[dict[str, Any]] | None = None
        self._pending_cache_boundary = False
        self._cache_boundary_pending = True

    def plan(
        self,
        observation: dict[str, Any],
        step: int,
        tool_context: list[dict[str, Any]],
        source_steps_remaining: int = 0,
    ) -> dict[str, Any]:
        system = self._planning_system_prompt()
        payload = self._planning_payload(observation, step, tool_context, source_steps_remaining)
        contract = {
            **payload["action_contract"],
            "reflection_contract": payload["reflection_contract"],
        }
        schema = build_decision_response_schema(contract)
        user_content = json.dumps(payload, ensure_ascii=False)
        cache_boundary = self._cache_boundary_pending or step == 0
        self._pending_user_content = user_content
        self._pending_tool_context = list(tool_context)
        self._pending_cache_boundary = cache_boundary
        return self._request(
            system,
            user_content,
            max_tokens=PLANNING_MAX_TOKENS,
            response_schema=schema,
            messages=self._planning_request_messages(user_content),
            cache_boundary=cache_boundary,
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
        if self._pending_user_content is None or self._pending_tool_context is None:
            raise RuntimeError("repair_plan requires a pending plan request")
        system = self._planning_system_prompt()
        payload = self._planning_payload(observation, step, tool_context, source_steps_remaining)
        contract = {
            **payload["action_contract"],
            "reflection_contract": payload["reflection_contract"],
        }
        schema = build_decision_response_schema(contract)
        correction_content = json.dumps(
            {
                "instruction": (
                    "Correct the previous response once. Return only a valid JSON decision. "
                    "Preserve valid gameplay tool, action, and arguments; correct only the fields "
                    "named by contract_error. Do not invent evidence IDs or candidate IDs."
                ),
                "contract_error": contract_error,
            },
            ensure_ascii=False,
        )
        repair_messages = self._planning_request_messages(self._pending_user_content)
        repair_messages.extend(
            [
                {
                    "role": "assistant",
                    "content": json.dumps(invalid_decision, ensure_ascii=False),
                },
                {"role": "user", "content": correction_content},
            ]
        )
        return self._request(
            system,
            correction_content,
            max_tokens=PLANNING_MAX_TOKENS,
            response_schema=schema,
            messages=repair_messages,
            cache_boundary=self._pending_cache_boundary,
        )

    def _planning_system_prompt(self) -> str:
        mode_note = (
            "You are in QA mode. Source tools may be used only when the current action_contract explicitly allows them."
            if self.mode == "qa"
            else "You are in Player Exploring mode. Use only player-perceptible state and game actions."
        )
        # Keep the no-assist wording byte-identical: it is the cached prompt prefix, and
        # drift both costs cache hits and makes token metrics incomparable across runs.
        if self.charter.bridge_assist:
            executor_note = "The executor blends a bounded survival term into your vector every frame."
            control_note = (
                "Unity does NOT choose a chest, attract toward a chest, or enforce the requested heading. "
                "It DOES add rule-based avoidance to your vector every frame: "
                f"executed = normalize(your_vector + escape_vector * {self.charter.assist_survival_weight} * clamp01(0.35 + danger)). "
                "controller.commanded is what you asked for and controller.steering is what executed; a difference "
                "between them is the assist, not a game defect. The assist is bounded and does not path-plan, so you "
                "must still steer away from danger yourself."
            )
        else:
            executor_note = "The executor never silently corrects your vector."
            control_note = (
                "Unity does NOT automatically avoid enemies, choose a chest, attract toward a chest, enforce the "
                "requested heading, or alter your direction. It only holds your chosen vector every frame and detects events."
            )
        return f"""You are an autonomous gameplay QA agent for a Vampire Survivors-style Unity game.
{mode_note}
The bridge_clock is a synchronization state, not a menu command. paused_at_observation=true with pause_reason=agent_decision_boundary means the game is already started and waiting for your next action. Never use start_game or wait to "unpause" it. Use observation.phase and action_contract as authoritative.
During normal direct control, Unity may keep holding your previous vector while this API call is pending. Choose exactly one bounded next tool call promptly.
Game actions: observe, start_game(index), direct_steer(x,y,duration,intent,target_id), wait(duration), select_upgrade(index), use_item(index), restart, return_to_menu.
QA-only tools: source_search(query), source_read(path,line_start,line_count).
The response MUST use one of action_contract.allowed_calls and arguments MUST always be a JSON object matching action_contract.required_arguments. For actions without arguments, return an empty object {{}}. Prefer progress and coverage, react immediately to upgrade dialogs, and never invent an unavailable game action.
Follow the supplied test charter. A named heading is a long-term NET-PROGRESS goal, not a per-action axis lock. Lateral detours and temporary backtracking are allowed for survival and chest collection. {executor_note}
During active gameplay use direct_steer. You alone must decide whether to continue the requested heading, evade enemies, or approach a specific chest from world.visible_chests. Set target_id to that chest ID when collecting; otherwise use 0. Set intent to a short value such as explore, evade, collect_chest, reposition, or hold.
{control_note} Use world.threat_entities, danger_score, escape_vector, and chest relative vectors to choose x/y yourself.
When intent=collect_chest, aim x/y toward that target's relative_x/relative_y (normally the normalized target vector); do not claim collection while moving away from it. When danger is high, an evade vector should materially align with escape_vector. Choose full 2D movement, not only a cardinal axis.
The horizon can end early on a chest entering close-control range, chest collection, low health, danger spikes, stuck detection, level-up, death, or another event. Re-plan from event_state and controller state.
Act as a QA engineer while you play. Before every state-changing game action, state a concrete expected_effect. On the next planning step, compare the compact prior outcome with that expectation in reflection. Use action_contract.has_previous_transition, not the numeric step, to decide reflection.status. When it is false, use not_applicable with empty evidence_refs and candidate_id. When it is true, never use not_applicable. Return evidence_refs as an empty array; the runner deterministically inserts the latest allowed transition IDs before validation. uncertain may describe an unresolved risk and may leave candidate_id empty. unexpected always requires a stable non-empty candidate_id.
Keep navigation reasoning in hypothesis and QA findings in qa_observation. An unexpected result starts a hypothesis; mark reproduction_attempted only when you deliberately repeated a relevant setup/action. A candidate becomes confirmed only after its own reproduction path, never from hidden evaluator data.
For direct_steer and wait, normally request a duration no greater than {self.plan_horizon_seconds:.3f} simulation seconds.
Return one JSON object only with keys: plan, hypothesis, qa_observation, tool, action, arguments, expected_effect, reflection.
Keep plan, hypothesis, qa_observation, expected_effect, and reflection.summary concise. tool must be game, source_search, or source_read. Player mode must always use game.
QA also means judging each observation on its own, not only action versus effect. "Consistent" here never means that a value changed plausibly over time; it means the numbers inside one observation agree with each other.
On every step, pick one numeric relationship in the current observation and actually compute it before you answer. Candidates: a field that summarizes two others as a ratio or fraction, a part against its whole, a count against the list it counts, an offset between two entities against the positions it is derived from. In qa_observation state the fields you chose, the value you computed from them, and the value the observation reports, in that order, even when they agree. Do not skip this because the transition looked normal: a matched transition says nothing about whether the state itself is valid. Advisory summaries such as a danger score are scores rather than invariants; do not check or report them.
If the computed value and the reported value disagree, or a fraction falls outside 0 to 1, or a count is negative, or a part exceeds its whole, that is a QA finding. Set reflection.status=unexpected with a short stable candidate_id, keep the two values in qa_observation, recheck the same fields on later observations, and set reproduction_attempted=true when you do."""

    def _planning_payload(
        self,
        observation: dict[str, Any],
        step: int,
        tool_context: list[dict[str, Any]],
        source_steps_remaining: int,
    ) -> dict[str, Any]:
        action_contract = build_action_contract(
            observation, self.mode, self.charter, source_steps_remaining
        )
        previous_transition = tool_context[-1] if tool_context else None
        reflection_contract = build_reflection_contract(previous_transition)
        action_contract["has_previous_transition"] = (
            reflection_contract["has_previous_transition"]
        )
        prior_outcome = (
            compact_planning_transition(
                previous_transition,
                include_latest_details=True,
            )
            if previous_transition
            else None
        )
        source_tool_result = None
        if isinstance(prior_outcome, dict):
            source_tool_result = prior_outcome.pop("source_tool_result", None)
        payload = {
            "step": step,
            "observation": compact_observation(observation),
            "action_contract": action_contract,
            "reflection_contract": reflection_contract,
            "prior_outcome": prior_outcome,
        }
        if source_tool_result is not None:
            payload["source_tool_result"] = source_tool_result
        return payload

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
            system,
            json.dumps(agent_context, ensure_ascii=False),
            max_tokens=1400,
            include_planning_history=False,
        )

    def _planning_context_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self._planning_system_prompt()},
            {
                "role": "system",
                "content": json.dumps(
                    {"test_charter": self.charter.as_dict()},
                    ensure_ascii=False,
                ),
            },
            {
                "role": "system",
                "content": json.dumps(
                    {"checkpoint_summary": self._planning_history.checkpoint_summary()},
                    ensure_ascii=False,
                ),
            },
        ]

    def _planning_request_messages(self, current_user: str) -> list[dict[str, str]]:
        messages = self._planning_context_messages()
        messages.extend(self._planning_history.messages())
        messages.append({"role": "user", "content": current_user})
        return messages

    def commit_plan(self, decision: dict[str, Any]) -> None:
        if self._pending_user_content is None or self._pending_tool_context is None:
            raise RuntimeError("commit_plan requires a pending plan request")
        rolled_over = self._planning_history.commit(
            self._pending_user_content,
            json.dumps(decision, ensure_ascii=False),
            self._pending_tool_context,
        )
        self._pending_user_content = None
        self._pending_tool_context = None
        self._pending_cache_boundary = False
        self._cache_boundary_pending = rolled_over

    def _request(
        self,
        system: str,
        user: str,
        max_tokens: int = PLANNING_MAX_TOKENS,
        response_schema: dict[str, Any] | None = None,
        messages: list[dict[str, str]] | None = None,
        include_planning_history: bool = True,
        cache_boundary: bool = False,
    ) -> dict[str, Any]:
        self._last_model_content = None
        if messages is None:
            if include_planning_history:
                messages = self._planning_request_messages(user)
            else:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
        body = {
            "model": self.model,
            "messages": messages,
            "response_format": self._response_format(response_schema),
        }
        if is_reasoning_model(self.model):
            body["max_completion_tokens"] = max(max_tokens, REASONING_OUTPUT_FLOOR)
            if self.reasoning_effort:
                body["reasoning_effort"] = self.reasoning_effort
        else:
            body["max_tokens"] = max_tokens
        if self.api_url.startswith("https://api.openai.com/"):
            body["prompt_cache_key"] = f"vsc-gameplay-qa-{self.mode}-{self.model}-v5"
        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        request_started = self.monotonic()
        billed: dict[str, int] = {}
        try:
            response_body, choice = self._complete(request, body, max_tokens, billed)
        finally:
            # Publish usage even when the call raises. A truncated attempt was
            # billed, and the caller drains this from its own finally.
            self.last_usage = self._session_usage(billed, request_started, cache_boundary)
        content = choice["message"]["content"]
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
        model_content = str(content)
        self._last_model_content = model_content
        return self._parse_json(model_content)

    def _session_usage(
        self,
        billed: dict[str, int],
        request_started: float,
        cache_boundary: bool,
    ) -> dict[str, int]:
        usage = dict(billed)
        if not usage and self._http_attempts == 0:
            # Nothing was billed (e.g. the request never reached the provider);
            # no attempted transport means there is no call to audit.
            return usage
        usage["latency_ms"] = max(0, round((self.monotonic() - request_started) * 1000))
        # request_count stays 1: one logical planning call, however many HTTP
        # round trips it took, so calls/mean_latency stay comparable across runs.
        usage["request_count"] = 1
        usage["cache_boundary"] = int(bool(cache_boundary))
        usage["llm_retries"] = self._retry_count
        usage["llm_retry_wait_ms"] = round(self._retry_wait_seconds * 1000)
        usage["llm_http_attempts"] = self._http_attempts
        usage["llm_completion_requests"] = self._completion_requests
        return usage

    def _complete(
        self,
        request: urllib.request.Request,
        body: dict[str, Any],
        max_tokens: int,
        billed: dict[str, int],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Get one finished completion, retrying once if the model ran out of room.

        The truncation retry has its own budget, separate from the transport
        attempt cap, so a rate limit early in the step cannot silently consume
        the one chance to recover from a truncated answer.

        `billed` accumulates across attempts: a truncated response is real spend
        and has to reach the report even though it was unusable.
        """
        reasoning = is_reasoning_model(self.model)
        budget_key = "max_completion_tokens" if reasoning else "max_tokens"
        floor = REASONING_OUTPUT_FLOOR if reasoning else 0
        caps = [max(max_tokens, floor)]
        retry_cap = max(min(max_tokens * 2, TRUNCATION_RETRY_MAX_TOKENS), floor * 2 if reasoning else 0)
        if retry_cap > caps[0]:
            caps.append(retry_cap)
        completions: list[int] = []
        total_retries = 0
        total_retry_wait_seconds = 0.0
        total_http_attempts = 0
        completion_requests = 0
        try:
            for cap in caps:
                body.pop("max_tokens", None)
                body.pop("max_completion_tokens", None)
                body[budget_key] = cap
                request.data = json.dumps(body).encode("utf-8")
                try:
                    response_body = self._send_with_retries(request)
                finally:
                    completion_requests += 1
                    total_retries += self._retry_count
                    total_retry_wait_seconds += self._retry_wait_seconds
                    total_http_attempts += self._http_attempts
                usage = self._normalize_usage(response_body.get("usage") or {})
                for key, value in usage.items():
                    billed[key] = billed.get(key, 0) + value
                choice = response_body["choices"][0]
                if choice.get("finish_reason") != "length":
                    return response_body, choice
                completions.append(int(usage.get("completion_tokens", 0) or 0))
            raise LLMTruncationError(
                "LLM response was truncated at the output token limit "
                f"(attempted max_tokens {caps}; completion_tokens {completions})"
            )
        finally:
            self._completion_requests = completion_requests
            self._retry_count = total_retries
            self._retry_wait_seconds = total_retry_wait_seconds
            self._http_attempts = total_http_attempts

    def _send_with_retries(self, request: urllib.request.Request) -> dict[str, Any]:
        """Retry a transient failure, honoring the provider's own delay hint.

        Planning requests are side-effect-free until commit_plan(), so replaying
        the HTTP call is safe. Three separate bounds apply, and all three are
        needed: an attempt cap, a per-wait ceiling so one absurd hint cannot
        stall the run, and a cumulative budget so repeated hints cannot either.
        The per-wait ceiling must exceed the observed rate-limit windows -- the
        real 429s asked for up to 11.2s, and clamping below that would guarantee
        an immediate second rejection.
        """
        self._retry_count = 0
        self._retry_wait_seconds = 0.0
        self._http_attempts = 0
        budget_remaining = self.retry_budget_seconds
        for attempt in range(self.max_attempts):
            self._http_attempts += 1
            try:
                return self._send_once(request)
            except (LLMTransportError, LLMResponseError) as error:
                last_attempt = attempt >= self.max_attempts - 1
                if last_attempt or not _is_retryable(error):
                    raise self._describe_exhaustion(error)
                wait = min(
                    max(getattr(error, "retry_after", None) or 0.0, float(2**attempt)),
                    MAX_RETRY_WAIT_SECONDS,
                )
                if wait > budget_remaining:
                    raise self._describe_exhaustion(
                        error,
                        f"retry budget of {self.retry_budget_seconds:g}s exhausted; "
                        f"the provider asked for {wait:g}s more",
                    )
                budget_remaining -= wait
                self._retry_count += 1
                self._retry_wait_seconds += wait
                self.sleep(wait)
        raise LLMTransportError("LLM retry loop terminated unexpectedly")

    def _describe_exhaustion(self, error: Exception, extra: str = "") -> Exception:
        suffix = (
            f" [attempts={self._http_attempts}, retries={self._retry_count}, "
            f"slept={self._retry_wait_seconds:.1f}s]"
        )
        if extra:
            suffix = f" [{extra}]{suffix}"
        return type(error)(f"{error}{suffix}")

    def _send_once(self, request: urllib.request.Request) -> dict[str, Any]:
        """Perform one round trip, labelling every way it can fail.

        Previously only HTTPError was caught, so a connection reset, the 120s
        read timeout, or a truncated body surfaced as a bare unlabelled error.
        """
        try:
            with self._urlopen(request, timeout=120) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise _transport_error(
                f"LLM API HTTP {error.code}: {detail[:1000]}",
                retryable=error.code in RETRYABLE_HTTP_STATUSES,
                retry_after=parse_retry_after(getattr(error, "headers", None), detail),
            ) from error
        except urllib.error.URLError as error:
            # URLError is HTTPError's base class, so this must stay second.
            raise _transport_error(
                f"LLM API request failed: {error.reason}", retryable=True
            ) from error
        except TimeoutError as error:
            raise _transport_error(
                "LLM API request timed out after 120s", retryable=True
            ) from error
        try:
            response_body = json.loads(payload)
        except json.JSONDecodeError as error:
            # A mangled envelope is usually a truncated transfer, so allow a retry.
            raise _response_error(
                f"LLM API returned a non-JSON body ({len(payload)} bytes): {payload[:200]}",
                retryable=True,
            ) from error
        if not isinstance(response_body, dict) or not response_body.get("choices"):
            # A well-formed envelope with no choices is a real provider answer,
            # not a glitch; repeating the request would repeat the answer.
            raise _response_error(
                f"LLM API response carried no choices: {payload[:200]}", retryable=False
            )
        return response_body

    def _response_format(self, response_schema: dict[str, Any] | None) -> dict[str, Any]:
        # Structured Outputs is an endpoint capability, not a gpt-4o-mini one. Pinning
        # it to that model silently dropped every other OpenAI model to json_object,
        # so swapping models changed schema enforcement at the same time as capability
        # and made the two impossible to tell apart. A model that cannot do it returns
        # a 400, which is loud and deliberately not retried.
        if response_schema is not None and self.api_url.startswith("https://api.openai.com/"):
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

    def take_last_model_content(self) -> str | None:
        content = self._last_model_content
        self._last_model_content = None
        return content

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
        normalized["uncached_prompt_tokens"] = max(
            0,
            normalized["prompt_tokens"] - normalized["cached_tokens"],
        )
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
