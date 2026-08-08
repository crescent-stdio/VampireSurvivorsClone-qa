from __future__ import annotations

from dataclasses import dataclass, replace
import threading
from typing import Protocol

from qa_llm_agent.actions import PolicyAction, UnityAction, to_unity_action
from qa_llm_agent.observation import QaObservation
from qa_llm_agent.policy import PolicyResult
from qa_llm_agent.scheduler import Trigger


class Policy(Protocol):
    def decide(
        self, observation: QaObservation, trigger: Trigger, requested_tick: int
    ) -> PolicyResult: ...


class AsyncPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class DecisionRecord:
    result: PolicyResult
    applied_tick: int | None = None


@dataclass(frozen=True)
class _Request:
    observation: QaObservation
    trigger: Trigger
    requested_tick: int


class AsyncPolicyDriver:
    def __init__(self, policy: Policy) -> None:
        self._policy = policy
        self._condition = threading.Condition()
        self._pending: _Request | None = None
        self._cached_action = PolicyAction(movement_x=0.0, movement_y=0.0, intent="initial no-op")
        self._records: list[DecisionRecord] = []
        self._unapplied_record: int | None = None
        self._error: Exception | None = None
        self._stopping = False
        self._worker = threading.Thread(target=self._run, name="qa-openai-policy", daemon=True)
        self._worker.start()

    def submit(self, observation: QaObservation, trigger: Trigger, requested_tick: int) -> None:
        with self._condition:
            if self._stopping:
                raise AsyncPolicyError("The asynchronous policy driver is closed.")
            self._pending = _Request(observation, trigger, requested_tick)
            self._condition.notify()

    def action_for(self, observation: QaObservation, applied_tick: int) -> UnityAction:
        with self._condition:
            if self._error is not None:
                raise AsyncPolicyError("The OpenAI policy worker failed.") from self._error
            action = self._cached_action
            if self._unapplied_record is not None:
                index = self._unapplied_record
                self._records[index] = replace(self._records[index], applied_tick=applied_tick)
                self._unapplied_record = None
        return to_unity_action(action, observation)

    def decisions(self) -> tuple[DecisionRecord, ...]:
        with self._condition:
            return tuple(self._records)

    def close(self) -> None:
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify_all()
        self._worker.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                request = self._pending
                self._pending = None
            try:
                result = self._policy.decide(
                    request.observation,
                    request.trigger,
                    request.requested_tick,
                )
            except Exception as error:
                with self._condition:
                    self._error = error
                    self._pending = None
                continue
            with self._condition:
                self._cached_action = result.action
                self._records.append(DecisionRecord(result=result))
                self._unapplied_record = len(self._records) - 1
