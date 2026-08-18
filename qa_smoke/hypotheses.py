from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal


HypothesisStatus = Literal[
    "hypothesis", "reproducing", "confirmed", "rejected", "uncertain"
]


@dataclass
class HypothesisState:
    candidate_id: str
    statement: str
    status: HypothesisStatus
    evidence_refs: list[str] = field(default_factory=list)
    reproduction_observations: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "statement": self.statement,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "reproduction_observations": self.reproduction_observations,
        }


class HypothesisTracker:
    def __init__(self) -> None:
        self._states: dict[str, HypothesisState] = {}

    def observe(self, candidate: dict[str, Any]) -> HypothesisState:
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        statement = str(candidate.get("statement") or "").strip()
        reflection_status = str(candidate.get("reflection_status") or "").strip()
        if not candidate_id:
            raise ValueError("candidate_id must not be empty")
        if not statement:
            raise ValueError("candidate statement must not be empty")
        if reflection_status not in {"matched", "unexpected", "uncertain"}:
            raise ValueError(f"unsupported reflection_status: {reflection_status}")
        evidence_refs = [
            str(reference)
            for reference in candidate.get("evidence_refs") or []
            if str(reference)
        ]
        reproduction_attempted = bool(candidate.get("reproduction_attempted", False))
        current = self._states.get(candidate_id)

        if current is None:
            initial_status: HypothesisStatus = (
                "hypothesis"
                if reflection_status == "unexpected"
                else "uncertain"
                if reflection_status == "uncertain"
                else "rejected"
            )
            current = HypothesisState(candidate_id, statement, initial_status)
            self._states[candidate_id] = current
        else:
            current.statement = statement
            if reflection_status == "matched":
                current.status = "rejected"
            elif reflection_status == "uncertain":
                current.status = "uncertain"
            elif current.status == "hypothesis" and reproduction_attempted:
                current.status = "reproducing"
                current.reproduction_observations += 1
            elif current.status == "reproducing" and reproduction_attempted:
                current.status = "confirmed"
                current.reproduction_observations += 1

        current.evidence_refs = list(
            dict.fromkeys([*current.evidence_refs, *evidence_refs])
        )
        return replace(current, evidence_refs=list(current.evidence_refs))

    def observe_decision(self, decision: dict[str, Any]) -> HypothesisState | None:
        reflection = decision.get("reflection") or {}
        if not isinstance(reflection, dict):
            return None
        candidate_id = str(reflection.get("candidate_id") or "").strip()
        status = str(reflection.get("status") or "").strip()
        if not candidate_id or status not in {"matched", "unexpected", "uncertain"}:
            return None
        statement = str(
            decision.get("qa_observation") or reflection.get("summary") or ""
        ).strip()
        return self.observe(
            {
                "candidate_id": candidate_id,
                "statement": statement,
                "reflection_status": status,
                "evidence_refs": reflection.get("evidence_refs") or [],
                "reproduction_attempted": reflection.get(
                    "reproduction_attempted", False
                ),
            }
        )

    def confirmed(self) -> list[dict[str, Any]]:
        return [
            state.as_dict()
            for state in self._states.values()
            if state.status == "confirmed"
        ]

    def snapshot(self) -> list[dict[str, Any]]:
        return [state.as_dict() for state in self._states.values()]
