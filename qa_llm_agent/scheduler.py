from __future__ import annotations

from enum import Enum
import threading

from qa_llm_agent.observation import LevelPhase, QaObservation


class Trigger(str, Enum):
    INITIAL = "initial"
    PERIODIC = "periodic"
    PHASE_CHANGED = "phase_changed"


class DecisionScheduler:
    def __init__(self, period_seconds: float = 2.0, attempt_budget: int = 90) -> None:
        if period_seconds <= 0:
            raise ValueError("Decision period must be positive.")
        if attempt_budget <= 0:
            raise ValueError("Attempt budget must be positive.")
        self.period_seconds = period_seconds
        self.attempt_budget = attempt_budget
        self._last_request_time: float | None = None
        self._last_phase: LevelPhase | None = None
        self._attempts = 0
        self._attempt_lock = threading.Lock()

    @property
    def attempts(self) -> int:
        with self._attempt_lock:
            return self._attempts

    @property
    def can_attempt(self) -> bool:
        return self.attempts < self.attempt_budget

    def record_attempt(self) -> int:
        with self._attempt_lock:
            if self._attempts >= self.attempt_budget:
                raise RuntimeError("OpenAI attempt budget is exhausted.")
            self._attempts += 1
            return self._attempts

    def next_trigger(self, observation: QaObservation) -> Trigger | None:
        if self._last_request_time is None:
            self._last_request_time = observation.elapsed_seconds
            self._last_phase = observation.phase
            return Trigger.INITIAL

        if observation.phase != self._last_phase:
            self._last_phase = observation.phase
            self._last_request_time = observation.elapsed_seconds
            return Trigger.PHASE_CHANGED

        if observation.elapsed_seconds - self._last_request_time >= self.period_seconds:
            self._last_request_time = observation.elapsed_seconds
            return Trigger.PERIODIC
        return None
