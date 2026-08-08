from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Callable, Protocol

from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from qa_llm_agent.actions import PolicyAction
from qa_llm_agent.observation import QaObservation
from qa_llm_agent.scheduler import DecisionScheduler, Trigger


PROMPT_VERSION = "qa-llm/v1"


class ResponsesApi(Protocol):
    def parse(self, **kwargs): ...


class OpenAiClient(Protocol):
    responses: ResponsesApi


class PolicySchemaError(RuntimeError):
    pass


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class PolicyResult:
    action: PolicyAction
    response_id: str
    latency_seconds: float
    usage: TokenUsage
    attempt_count: int
    trigger: Trigger
    requested_tick: int


class OpenAiPolicy:
    def __init__(
        self,
        *,
        client: OpenAiClient,
        model: str,
        scheduler: DecisionScheduler,
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.model = model
        self.scheduler = scheduler
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep
        self.monotonic = monotonic

    def decide(
        self,
        observation: QaObservation,
        trigger: Trigger,
        requested_tick: int,
    ) -> PolicyResult:
        started = self.monotonic()
        attempts_before_request = self.scheduler.attempts
        for retry_index in range(3):
            self.scheduler.record_attempt()
            try:
                response = self.client.responses.parse(
                    model=self.model,
                    instructions=(
                        "Control movement for a deterministic gameplay QA episode. "
                        "Choose a finite unit-square movement vector and briefly state the observable intent. "
                        "Ability selection is handled locally and must not be included."
                    ),
                    input=json.dumps(observation.as_dict(), separators=(",", ":"), sort_keys=True),
                    text_format=PolicyAction,
                    reasoning={"effort": "none"},
                    store=False,
                    timeout=self.timeout_seconds,
                )
            except Exception as error:
                if retry_index >= 2 or not _is_transient(error):
                    raise
                self.sleep(float(2**retry_index))
                continue

            parsed = getattr(response, "output_parsed", None)
            if not isinstance(parsed, PolicyAction):
                raise PolicySchemaError("OpenAI returned no valid structured policy action.")
            usage = getattr(response, "usage", None)
            return PolicyResult(
                action=parsed,
                response_id=str(getattr(response, "id", "")),
                latency_seconds=max(0.0, self.monotonic() - started),
                usage=TokenUsage(
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                    total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
                ),
                attempt_count=self.scheduler.attempts - attempts_before_request,
                trigger=trigger,
                requested_tick=requested_tick,
            )
        raise RuntimeError("OpenAI retry loop terminated unexpectedly.")


def _is_transient(error: Exception) -> bool:
    if isinstance(error, (APITimeoutError, APIConnectionError, RateLimitError)):
        return True
    return isinstance(error, APIStatusError) and error.status_code >= 500
