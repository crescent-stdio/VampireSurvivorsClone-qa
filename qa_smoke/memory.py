from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any


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


def sanitize_agent_channel(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: sanitize_agent_channel(item)
            for key, item in value.items()
            if str(key).lower() not in EVALUATOR_PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [sanitize_agent_channel(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_agent_channel(item) for item in value]
    return value


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
