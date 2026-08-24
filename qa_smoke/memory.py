from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter
from functools import lru_cache
from typing import Any

from .scenarios import load_scenarios, load_v4_ground_truth, load_v4_scenarios


EVALUATOR_PRIVATE_KEYS = frozenset(
    {
        "fault_id",
        "ground_truth",
        "expected_behavior",
        "bug_id",
        "oracle",
        "oracle_verdict",
        "coverage_status",
        "evaluator",
        "manifest",
        "verdict",
    }
)

_PRIVATE_KEY_MARKERS = EVALUATOR_PRIVATE_KEYS | frozenset(
    {
        "context_ref",
        "context_refs",
        "injection_log",
        "injection_logs",
        "source_code",
        "source_path",
        "source_paths",
        "source_tool",
        "source_tools",
    }
)
_PRIVATE_TEXT_MARKERS = (
    "injected",
    "injecting",
    "fault injection",
    "ground truth",
    "expected behavior",
    "evaluator state",
    "injection log",
    "context ref",
    "qafaultinjection",
)
_SOURCE_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'])(?:[a-z]:)?(?:[/\\]|(?:[\w.-]+[/\\])+[\w.-]+\.(?:cs|py|json|md))",
    re.IGNORECASE,
)
_DROP = object()


def _normalize_private_key(key: object) -> str:
    """Make camelCase, PascalCase, and separator variants comparable."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _is_private_key(key: str) -> bool:
    return key in _PRIVATE_KEY_MARKERS or any(
        marker in key
        for marker in (
            "fault",
            "ground_truth",
            "expected_behavior",
            "bug_id",
            "oracle",
            "evaluator",
            "context_ref",
            "injection",
            "source_code",
            "source_path",
            "source_tool",
        )
    )


@lru_cache(maxsize=1)
def _known_private_text() -> tuple[str, ...]:
    """Load only private identifiers used to reject accidental prompt leakage."""
    markers: set[str] = set(_PRIVATE_TEXT_MARKERS)
    try:
        for scenario in load_scenarios():
            if scenario.ground_truth.fault_id:
                markers.add(scenario.ground_truth.fault_id.lower())
        for scenario in load_v4_scenarios():
            markers.update(reference.lower() for reference in scenario.context_refs)
        for entry in load_v4_ground_truth().values():
            fault_id = entry.get("fault_id")
            if isinstance(fault_id, str) and fault_id:
                markers.add(fault_id.lower())
    except ValueError:
        # A malformed local configuration must not weaken the static private-key gate.
        pass
    return tuple(sorted(marker for marker in markers if marker))


def _contains_private_text(value: str) -> bool:
    lowered = value.lower()
    return bool(
        _SOURCE_PATH_PATTERN.search(value)
        or any(marker in lowered for marker in _known_private_text())
    )


def _sanitize_agent_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _normalize_private_key(key)
            if _is_private_key(normalized_key):
                continue
            clean = _sanitize_agent_value(item)
            if clean is not _DROP:
                sanitized[key] = clean
        return sanitized
    if isinstance(value, list):
        return [clean for item in value if (clean := _sanitize_agent_value(item)) is not _DROP]
    if isinstance(value, tuple):
        return [clean for item in value if (clean := _sanitize_agent_value(item)) is not _DROP]
    if isinstance(value, str) and _contains_private_text(value):
        return _DROP
    return copy.deepcopy(value)


def sanitize_agent_channel(value: Any) -> Any:
    """Remove evaluator-only values from nested payloads and free-text fields."""
    sanitized = _sanitize_agent_value(value)
    return None if sanitized is _DROP else sanitized


def sanitize_error_type(value: str | None) -> str | None:
    """Keep only a bounded public exception class identifier."""

    if value is None:
        return None
    if (
        0 < len(value) <= 128
        and value.isascii()
        and (value[0].isalpha() or value[0] == "_")
        and all(character.isalnum() or character == "_" for character in value)
    ):
        return value
    return "Exception"


class SessionMemory:
    def __init__(self, recent_limit: int = 6, token_budget: int = 800) -> None:
        if recent_limit < 1:
            raise ValueError("recent_limit must be at least one")
        if token_budget < 64:
            raise ValueError("token_budget must be at least 64")
        self.recent_limit = recent_limit
        self.token_budget = token_budget
        self._recent: list[dict[str, Any]] = []
        self._compressed_count = 0
        self._visited_areas: list[str] = []
        self._milestones: list[str] = []
        self._event_counts: Counter[str] = Counter()
        self._acquisitions: list[dict[str, Any]] = []
        self._unresolved_hypotheses: dict[str, dict[str, Any]] = {}
        self._action_event_links: list[dict[str, Any]] = []

    @classmethod
    def from_transitions(
        cls,
        transitions: list[dict[str, Any]],
        recent_limit: int = 6,
        token_budget: int = 800,
    ) -> "SessionMemory":
        memory = cls(recent_limit=recent_limit, token_budget=token_budget)
        for transition in transitions:
            memory.add(transition)
        return memory

    def add(self, transition: dict[str, Any]) -> None:
        sanitized = sanitize_agent_channel(transition)
        if not isinstance(sanitized, dict):
            raise ValueError("transition must be a JSON object")
        self._recent.append(sanitized)
        if len(self._recent) > self.recent_limit:
            self._compress(self._recent.pop(0))

    def recent_transitions(self) -> list[dict[str, Any]]:
        return sanitize_agent_channel(self._recent)

    def summary(self) -> dict[str, Any]:
        summary = {
            "compressed_transition_count": self._compressed_count,
            "visited_areas": list(self._visited_areas),
            "milestones": list(self._milestones),
            "event_counts": dict(sorted(self._event_counts.items())),
            "acquisitions": list(self._acquisitions),
            "unresolved_hypotheses": list(self._unresolved_hypotheses.values()),
            "action_event_links": list(self._action_event_links),
        }
        return self._fit_budget(summary)

    def estimated_summary_tokens(self) -> int:
        return self._estimated_tokens(self.summary())

    def _compress(self, transition: dict[str, Any]) -> None:
        self._compressed_count += 1
        observation = transition.get("observation") or transition
        if not isinstance(observation, dict):
            observation = {}
        decision = transition.get("decision") or transition.get("game_action") or {}
        if not isinstance(decision, dict):
            decision = {}
        player = observation.get("player") or transition.get("player") or {}
        progress = observation.get("progress") or transition.get("progress") or {}
        event = observation.get("event_state") or transition.get("event_state") or {}

        position = player.get("position") or {}
        if "x" in position and "y" in position:
            area = self._area_name(
                float(position.get("x", 0.0) or 0.0),
                float(position.get("y", 0.0) or 0.0),
            )
            self._append_unique(self._visited_areas, area, limit=24)

        level = int(player.get("level", progress.get("level", 0)) or 0)
        if level > 1:
            self._append_unique(self._milestones, f"level:{level}", limit=24)
        scene = str(observation.get("scene") or transition.get("scene") or "")
        if scene:
            self._append_unique(self._milestones, f"scene:{scene[:80]}", limit=24)
        action = str(decision.get("action") or "")
        if action in {"select_upgrade", "use_item", "restart"}:
            acquisition = {
                "action": action,
                "decision_id": str(decision.get("decision_id") or ""),
                "index": (decision.get("arguments") or {}).get("index"),
            }
            self._acquisitions.append(acquisition)
            self._acquisitions = self._acquisitions[-16:]

        event_type = str(event.get("type") or "")
        if event_type:
            self._event_counts[event_type] += 1
            event_id = str(event.get("event_id") or "")
            caused_by = str(event.get("caused_by_command_id") or "")
            command_id = str(
                observation.get("command_id") or transition.get("command_id") or ""
            )
            if event_id and caused_by and caused_by == command_id:
                self._action_event_links.append(
                    {
                        "action": action,
                        "decision_id": str(decision.get("decision_id") or ""),
                        "event_type": event_type,
                        "event_id": event_id,
                    }
                )
                self._action_event_links = self._action_event_links[-16:]

        hypothesis = decision.get("hypothesis_state") or transition.get(
            "hypothesis_state"
        )
        if isinstance(hypothesis, dict):
            candidate_id = str(hypothesis.get("candidate_id") or "")
            status = str(hypothesis.get("status") or "")
            if candidate_id and status in {"hypothesis", "reproducing", "uncertain"}:
                self._unresolved_hypotheses[candidate_id] = {
                    "candidate_id": candidate_id,
                    "statement": str(hypothesis.get("statement") or "")[:240],
                    "status": status,
                    "evidence_refs": [
                        str(reference)
                        for reference in hypothesis.get("evidence_refs") or []
                    ][-8:],
                }
            elif candidate_id and status in {"confirmed", "rejected"}:
                self._unresolved_hypotheses.pop(candidate_id, None)

    def _fit_budget(self, summary: dict[str, Any]) -> dict[str, Any]:
        fitted = sanitize_agent_channel(summary)
        for key in ("milestones", "visited_areas", "acquisitions", "action_event_links"):
            while self._estimated_tokens(fitted) > self.token_budget and len(fitted[key]) > 1:
                fitted[key].pop(0)
        while (
            self._estimated_tokens(fitted) > self.token_budget
            and fitted["event_counts"]
        ):
            fitted["event_counts"].pop(next(iter(fitted["event_counts"])))
        if self._estimated_tokens(fitted) > self.token_budget:
            for hypothesis in fitted["unresolved_hypotheses"]:
                hypothesis["statement"] = hypothesis["statement"][:80]
                hypothesis["evidence_refs"] = hypothesis["evidence_refs"][-2:]
        if self._estimated_tokens(fitted) > self.token_budget:
            fitted["milestones"] = []
            fitted["visited_areas"] = []
            fitted["acquisitions"] = []
            fitted["action_event_links"] = fitted["action_event_links"][-1:]
        return fitted

    @staticmethod
    def _estimated_tokens(value: dict[str, Any]) -> int:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return math.ceil(len(encoded) / 4)

    @staticmethod
    def _area_name(x: float, y: float) -> str:
        return f"grid:{math.floor(x / 20.0)}:{math.floor(y / 20.0)}"

    @staticmethod
    def _append_unique(values: list[str], value: str, limit: int) -> None:
        if value not in values:
            values.append(value)
        if len(values) > limit:
            del values[: len(values) - limit]


class PlanningHistory:
    """Store committed planner exchanges and periodically checkpoint older transitions."""

    MAX_ACCEPTED_EXCHANGES = 24
    RETAINED_EXCHANGES = 12

    def __init__(self) -> None:
        self._messages: list[dict[str, str]] = []
        self._accepted_count = 0
        self._transition_history: list[dict[str, Any]] = []
        self._checkpoint: dict[str, Any] = SessionMemory(
            recent_limit=self.RETAINED_EXCHANGES
        ).summary()

    def messages(self) -> list[dict[str, str]]:
        return copy.deepcopy(self._messages)

    def checkpoint_summary(self) -> dict[str, Any]:
        return copy.deepcopy(self._checkpoint)

    def commit(
        self,
        user_content: str,
        assistant_content: str,
        tool_context: list[dict[str, Any]],
    ) -> bool:
        self._remember_transitions(tool_context)
        self._messages.extend(
            [
                {"role": "user", "content": str(user_content)},
                {"role": "assistant", "content": str(assistant_content)},
            ]
        )
        self._accepted_count += 1
        rolled_over = self._should_roll_over(self._accepted_count)
        if rolled_over:
            self._checkpoint = SessionMemory.from_transitions(
                self._transition_history,
                recent_limit=self.RETAINED_EXCHANGES,
            ).summary()
            self._messages = self._messages[-(self.RETAINED_EXCHANGES * 2) :]
        return rolled_over

    @classmethod
    def _should_roll_over(cls, accepted_count: int) -> bool:
        first_rollover = cls.MAX_ACCEPTED_EXCHANGES + 1
        return accepted_count >= first_rollover and (
            accepted_count - first_rollover
        ) % cls.RETAINED_EXCHANGES == 0

    def _remember_transitions(self, tool_context: list[dict[str, Any]]) -> None:
        if not tool_context:
            return
        copied = copy.deepcopy(tool_context)
        if (
            len(copied) >= len(self._transition_history)
            and copied[: len(self._transition_history)] == self._transition_history
        ):
            self._transition_history = copied
            return
        self._transition_history.append(copied[-1])
