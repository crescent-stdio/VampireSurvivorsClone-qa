from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from typing import Iterable, Mapping
from uuid import uuid4

from qa_llm_agent.async_driver import DecisionRecord
from qa_llm_agent.policy import PROMPT_VERSION, TokenUsage


DECISION_SCHEMA = "qa-llm-decision/v1"
RUN_SCHEMA = "qa-llm-run/v1"
FAILURE_SCHEMA = "qa-llm-failure/v1"


def write_episode_artifacts(
    episode_directory: Path,
    *,
    seed: int,
    model: str,
    records: Iterable[DecisionRecord],
    api_attempts: int,
    summary: Mapping[str, object],
) -> tuple[Path, Path]:
    decisions = tuple(records)
    decision_path = episode_directory / "llm-decisions.jsonl"
    run_path = episode_directory / "llm-run.json"
    if decision_path.exists() or run_path.exists():
        raise FileExistsError("LLM episode artifacts already exist.")

    decision_lines = "".join(
        json.dumps(_decision_payload(seed, model, record), sort_keys=True, separators=(",", ":")) + "\n"
        for record in decisions
    )
    total_usage = _sum_usage(decisions)
    run_payload = {
        "schema": RUN_SCHEMA,
        "seed": seed,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "api_attempts": api_attempts,
        "decision_count": len(decisions),
        "input_tokens": total_usage.input_tokens,
        "output_tokens": total_usage.output_tokens,
        "total_tokens": total_usage.total_tokens,
        "outcome": summary.get("Outcome"),
        "failure_reason": summary.get("FailureReason", ""),
    }
    _atomic_write(decision_path, decision_lines)
    _atomic_write(run_path, json.dumps(run_payload, sort_keys=True, separators=(",", ":")) + "\n")
    return decision_path, run_path


def write_failure_artifact(
    artifact_root: Path,
    *,
    seed: int,
    model: str,
    error: Exception,
    secrets: Iterable[str] = (),
) -> Path:
    failure_directory = artifact_root / "llm-failures"
    failure_directory.mkdir(parents=True, exist_ok=True)
    failure_path = failure_directory / f"failure-{seed:08d}-{time.time_ns()}-{uuid4().hex}.json"
    message = _safe_error_message(str(error), secrets)
    payload = {
        "schema": FAILURE_SCHEMA,
        "seed": seed,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "error_type": type(error).__name__,
        "message": message,
    }
    _atomic_write(failure_path, json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return failure_path


def _decision_payload(seed: int, model: str, record: DecisionRecord) -> dict[str, object]:
    usage = record.usage if isinstance(record.usage, TokenUsage) else TokenUsage(0, 0, 0)
    return {
        "schema": DECISION_SCHEMA,
        "seed": seed,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "trigger": record.trigger,
        "source": record.source,
        "requested_tick": record.requested_tick,
        "applied_tick": record.applied_tick,
        "observation": record.observation.as_dict(),
        "action": {
            "movement_x": record.action.continuous[0],
            "movement_y": record.action.continuous[1],
            "ability_choice": record.action.ability_choice,
            "discrete_action": record.action.discrete[0],
            "intent": record.action.intent,
        },
        "response_id": record.response_id,
        "latency_seconds": record.latency_seconds,
        "usage": asdict(usage),
        "attempt_count": record.attempt_count,
    }


def _sum_usage(records: Iterable[DecisionRecord]) -> TokenUsage:
    input_tokens = output_tokens = total_tokens = 0
    for record in records:
        if not isinstance(record.usage, TokenUsage):
            continue
        input_tokens += record.usage.input_tokens
        output_tokens += record.usage.output_tokens
        total_tokens += record.usage.total_tokens
    return TokenUsage(input_tokens, output_tokens, total_tokens)


def _safe_error_message(message: str, secrets: Iterable[str]) -> str:
    sanitized = message
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    lowered = sanitized.lower()
    for marker in ("raw body", "response body", "http body"):
        if marker in lowered:
            start = lowered.index(marker)
            sanitized = sanitized[:start] + "response details omitted"
            lowered = sanitized.lower()
    return sanitized[:500]


def _atomic_write(path: Path, contents: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
