from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, RateLimitError

from qa_llm_agent.actions import PolicyAction
from qa_llm_agent.observation import decode_observation
from qa_llm_agent.policy import OpenAiPolicy, PolicySchemaError
from qa_llm_agent.scheduler import DecisionScheduler, Trigger


class FakeResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.responses = FakeResponses(outcomes)


def observation():
    return decode_observation([0.0] * 36)


def response(parsed=PolicyAction(movement_x=0.25, movement_y=-0.5, intent="kite")):
    return SimpleNamespace(
        id="resp_123",
        output_parsed=parsed,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120),
    )


def test_policy_uses_responses_structured_output_without_storing_the_response() -> None:
    client = FakeClient([response()])
    scheduler = DecisionScheduler(attempt_budget=90)
    policy = OpenAiPolicy(client=client, model="gpt-5.6-terra", scheduler=scheduler)

    result = policy.decide(observation(), Trigger.INITIAL, requested_tick=3)

    assert result.action == PolicyAction(movement_x=0.25, movement_y=-0.5, intent="kite")
    assert result.response_id == "resp_123"
    assert result.usage.total_tokens == 120
    assert result.attempt_count == 1
    call = client.responses.calls[0]
    assert call["model"] == "gpt-5.6-terra"
    assert call["text_format"] is PolicyAction
    assert call["reasoning"] == {"effort": "none"}
    assert call["store"] is False
    assert call["timeout"] == 30.0


def test_policy_retries_429_twice_and_counts_every_attempt() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    rate_limit = lambda: RateLimitError(
        "limited", response=httpx.Response(429, request=request), body=None
    )
    client = FakeClient([rate_limit(), rate_limit(), response()])
    scheduler = DecisionScheduler(attempt_budget=90)
    sleeps = []
    policy = OpenAiPolicy(client=client, model="gpt-5.6-terra", scheduler=scheduler, sleep=sleeps.append)

    result = policy.decide(observation(), Trigger.PERIODIC, requested_tick=20)

    assert result.attempt_count == 3
    assert scheduler.attempts == 3
    assert sleeps == [1.0, 2.0]


def test_policy_does_not_retry_a_schema_violation() -> None:
    client = FakeClient([response(parsed=None), response()])
    scheduler = DecisionScheduler(attempt_budget=90)
    policy = OpenAiPolicy(client=client, model="gpt-5.6-terra", scheduler=scheduler)

    with pytest.raises(PolicySchemaError):
        policy.decide(observation(), Trigger.INITIAL, requested_tick=1)

    assert len(client.responses.calls) == 1
    assert scheduler.attempts == 1


def test_policy_raises_after_the_final_timeout() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    client = FakeClient([APITimeoutError(request=request) for _ in range(3)])
    scheduler = DecisionScheduler(attempt_budget=90)
    policy = OpenAiPolicy(
        client=client,
        model="gpt-5.6-terra",
        scheduler=scheduler,
        sleep=lambda _: None,
    )

    with pytest.raises(APITimeoutError):
        policy.decide(observation(), Trigger.INITIAL, requested_tick=1)

    assert scheduler.attempts == 3
