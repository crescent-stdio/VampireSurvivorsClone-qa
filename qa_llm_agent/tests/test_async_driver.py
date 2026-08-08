import threading
import time

from qa_llm_agent.actions import PolicyAction
from qa_llm_agent.async_driver import AsyncPolicyDriver
from qa_llm_agent.observation import decode_observation
from qa_llm_agent.policy import PolicyResult, TokenUsage
from qa_llm_agent.scheduler import Trigger


def observation(elapsed: float = 0.0):
    values = [0.0] * 36
    values[33] = elapsed / 600
    return decode_observation(values)


class BlockingPolicy:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.elapsed_requests = []

    def decide(self, state, trigger, requested_tick):
        self.elapsed_requests.append(state.elapsed_seconds)
        self.started.set()
        self.release.wait(timeout=2)
        return PolicyResult(
            action=PolicyAction(movement_x=1, movement_y=0, intent="move"),
            response_id="resp",
            latency_seconds=0.1,
            usage=TokenUsage(1, 1, 2),
            attempt_count=1,
            trigger=trigger,
            requested_tick=requested_tick,
        )


def test_driver_returns_cached_action_without_waiting_for_the_worker() -> None:
    policy = BlockingPolicy()
    driver = AsyncPolicyDriver(policy)
    driver.submit(observation(), Trigger.INITIAL, requested_tick=1)
    assert policy.started.wait(timeout=1)

    started = time.monotonic()
    action = driver.action_for(observation(), applied_tick=2)
    elapsed = time.monotonic() - started

    assert elapsed < 0.05
    assert action.continuous == (0.0, 0.0)
    policy.release.set()
    assert wait_until(lambda: driver.action_for(observation(), 3).continuous == (1.0, 0.0))
    assert driver.decisions()[0].requested_tick == 1
    assert driver.decisions()[0].applied_tick == 3
    assert driver.decisions()[0].source == "openai"
    driver.close()


def test_driver_coalesces_busy_requests_to_the_latest_observation() -> None:
    policy = BlockingPolicy()
    driver = AsyncPolicyDriver(policy)
    driver.submit(observation(0), Trigger.INITIAL, requested_tick=1)
    assert policy.started.wait(timeout=1)
    driver.submit(observation(1), Trigger.PERIODIC, requested_tick=10)
    driver.submit(observation(2), Trigger.PHASE_CHANGED, requested_tick=20)

    policy.release.set()
    assert wait_until(lambda: len(policy.elapsed_requests) == 2)

    assert policy.elapsed_requests == [0.0, 2.0]
    driver.close()


def test_driver_records_local_ability_selection_as_a_separate_immediate_decision() -> None:
    policy = BlockingPolicy()
    driver = AsyncPolicyDriver(policy)
    values = [0.0] * 36
    values[28] = 1.0
    values[35] = 1.0

    action = driver.action_for(decode_observation(values), applied_tick=7)

    assert action.discrete == (2,)
    record = driver.decisions()[0]
    assert record.source == "local_safety"
    assert record.requested_tick == 7
    assert record.applied_tick == 7
    assert record.action.ability_choice == 1
    driver.close()


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False
