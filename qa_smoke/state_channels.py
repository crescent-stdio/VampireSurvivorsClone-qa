from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from .memory import sanitize_agent_channel


_PRIVATE_KEYS = {"fault_id", "ground_truth", "bug_id", "expected_behavior"}
_ADVISORY_KEYS = {"danger_score", "escape_vector"}
_NON_AGENT_CHANNEL_KEYS = {"evaluator_state", "player_view", "agent_state", "harness_advisory"}
_PUBLIC_PLAYER_VIEW_FIELDS = (
    "health",
    "max_health",
    "health_ratio",
    "exp",
    "next_level_exp",
    "exp_ratio",
)


def _without_keys(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_keys(item, keys)
            for key, item in value.items()
            if key not in keys
        }
    if isinstance(value, list):
        return [_without_keys(item, keys) for item in value]
    return deepcopy(value)


def project_public_player_view(value: Any) -> dict[str, float]:
    """Keep only numeric displayed health and experience telemetry for model input."""
    source = value if isinstance(value, dict) else {}
    projection: dict[str, float] = {}
    for field in _PUBLIC_PLAYER_VIEW_FIELDS:
        candidate = source.get(field)
        if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
            continue
        numeric = float(candidate)
        if math.isfinite(numeric):
            projection[field] = numeric
    return projection


def build_state_channels(observation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Split a bridge observation without modifying or clamping its raw values."""

    raw = _without_keys(observation, _ADVISORY_KEYS)
    raw.pop("fault_id", None)
    raw.pop("ground_truth", None)
    advisory = {
        "danger_score": (observation.get("world") or {}).get("danger_score"),
        "escape_vector": deepcopy((observation.get("world") or {}).get("escape_vector")),
    }
    player_view = deepcopy(observation.get("player_view") or {})
    agent_state = _without_keys(raw, _PRIVATE_KEYS | _ADVISORY_KEYS | _NON_AGENT_CHANNEL_KEYS)
    agent_state = sanitize_agent_channel(agent_state)
    public_player_view = project_public_player_view(player_view)
    if public_player_view:
        agent_state["player_view"] = public_player_view
    return {
        "evaluator_state": raw,
        "player_view": player_view,
        "agent_state": agent_state,
        "harness_advisory": advisory,
    }


def build_agent_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """Return the observation shape safe to provide to a driver or LLM."""

    channels = build_state_channels(observation)
    return channels["agent_state"]
