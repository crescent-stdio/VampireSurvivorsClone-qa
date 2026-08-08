import json
from pathlib import Path

from qa_llm_agent.actions import UnityAction
from qa_llm_agent.artifacts import write_episode_artifacts, write_failure_artifact
from qa_llm_agent.async_driver import DecisionRecord
from qa_llm_agent.observation import decode_observation
from qa_llm_agent.policy import PROMPT_VERSION, TokenUsage
from qa_llm_agent.scheduler import Trigger


def test_episode_artifacts_record_decisions_ticks_usage_and_no_sensitive_payloads(tmp_path: Path) -> None:
    episode = tmp_path / "episode-00009301-test"
    episode.mkdir()
    record = DecisionRecord(
        source="openai",
        trigger=Trigger.INITIAL.value,
        observation=decode_observation([0.0] * 36),
        action=UnityAction((0.5, -0.25), (0,), -1, "kite", "openai"),
        requested_tick=1,
        applied_tick=4,
        response_id="resp_123",
        latency_seconds=0.25,
        usage=TokenUsage(10, 5, 15),
        attempt_count=1,
    )

    write_episode_artifacts(
        episode,
        seed=9301,
        model="gpt-5.6-terra",
        records=(record,),
        api_attempts=1,
        summary={"Outcome": 1, "FailureReason": ""},
    )

    decision = json.loads((episode / "llm-decisions.jsonl").read_text(encoding="utf-8"))
    run = json.loads((episode / "llm-run.json").read_text(encoding="utf-8"))
    assert decision["requested_tick"] == 1
    assert decision["applied_tick"] == 4
    assert decision["source"] == "openai"
    assert decision["response_id"] == "resp_123"
    assert decision["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert run["prompt_version"] == PROMPT_VERSION
    assert run["api_attempts"] == 1
    assert run["total_tokens"] == 15
    serialized = (episode / "llm-decisions.jsonl").read_text() + (episode / "llm-run.json").read_text()
    assert "OPENAI_API_KEY" not in serialized
    assert "raw_http" not in serialized
    assert "reasoning_content" not in serialized
    assert not list(episode.glob("*.tmp"))


def test_failure_artifact_redacts_the_api_key_and_omits_raw_response_data(tmp_path: Path) -> None:
    path = write_failure_artifact(
        tmp_path,
        seed=9302,
        model="gpt-5.6-terra",
        error=RuntimeError("request failed with sk-secret-value and raw body"),
        secrets=("sk-secret-value",),
    )

    contents = path.read_text(encoding="utf-8")
    failure = json.loads(contents)
    assert failure["error_type"] == "RuntimeError"
    assert "sk-secret-value" not in contents
    assert "[REDACTED]" in contents
    assert "raw body" not in contents
