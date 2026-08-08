import pytest

from qa_llm_agent.observation import LevelPhase, decode_observation
from qa_llm_agent.scheduler import DecisionScheduler, Trigger


def observation(elapsed_seconds: float, phase: LevelPhase = LevelPhase.EARLY):
    values = [0.0] * 36
    values[31] = phase.value / 4
    values[33] = elapsed_seconds / 600
    return decode_observation(values)


def test_scheduler_triggers_first_decision_period_and_phase_changes() -> None:
    scheduler = DecisionScheduler(period_seconds=2.0, attempt_budget=90)

    assert scheduler.next_trigger(observation(0.0)) is Trigger.INITIAL
    assert scheduler.next_trigger(observation(1.9)) is None
    assert scheduler.next_trigger(observation(2.0)) is Trigger.PERIODIC
    assert scheduler.next_trigger(observation(2.1, LevelPhase.MID)) is Trigger.PHASE_CHANGED


def test_scheduler_counts_every_api_attempt_against_the_budget() -> None:
    scheduler = DecisionScheduler(period_seconds=2.0, attempt_budget=2)

    assert scheduler.record_attempt() == 1
    assert scheduler.record_attempt() == 2
    assert scheduler.can_attempt is False
    with pytest.raises(RuntimeError, match="budget"):
        scheduler.record_attempt()
