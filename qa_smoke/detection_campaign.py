"""Track B action replay, blind inspection, checkpointing, and campaign orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

from .adapters import VampireSurvivorsAdapter
from .charter import TestCharter
from .detection_benchmark import (
    FAULT_IDS,
    PairScore,
    TraceEvaluation,
    benchmark_report_json,
    build_benchmark_report,
    render_benchmark_markdown,
    score_pair,
)
from .evaluation import evaluate_coverage, evaluate_oracle, evaluate_v4_oracle
from .inspector import (
    FINDINGS_SCHEMA,
    INSPECTOR_SYSTEM_PROMPT,
    INSPECTION_SCHEMA_V2,
    build_inspection_chunks,
    normalize_inspection_artifact,
)
from .planners import LLMPlanner, observation_phase
from .run import execute_game_action, parse_args as parse_run_args, run_session
from .scenarios import load_scenarios, load_v4_ground_truth, load_v4_scenarios


ACTION_REPLAY_SCHEMA = "qa-action-replay/v1"
CAMPAIGN_MANIFEST_SCHEMA = "qa-campaign-manifest/v1"
CHECKPOINT_SCHEMA = "qa-campaign-checkpoint/v1"
INSPECTION_AUDIT_SCHEMA = "qa-inspection-audit/v1"
STEERING_MODEL = "gpt-4o-mini"
INSPECTOR_MODEL = "gpt-5.6-luna"
INSPECTOR_EFFORT = "low"
SEEDS = (9101, 9102, 9103)
AUTONOMOUS_SEED = 9101
INSPECTION_REPETITIONS = 3
LOGICAL_INSPECTION_CALL_CAP = 700
PLANNED_MAX_LOGICAL_INSPECTION_CALLS = 693
MAX_HTTP_ATTEMPTS = 3
MAX_RETRY_SLEEP_SECONDS = 45.0
NEUTRAL_REACHABILITY_GOAL = (
    "Reach ordinary gameplay state needed for generic QA observation, while surviving when possible."
)


class CampaignContractError(RuntimeError):
    """Raised when campaign artifacts or fixed evaluation contracts do not match."""


class InspectionCallError(RuntimeError):
    """Carry failed-call audit counters without persisting provider response details."""

    def __init__(
        self,
        message: str,
        *,
        usage: Mapping[str, int],
        elapsed_seconds: float,
    ) -> None:
        super().__init__(message)
        self.usage = dict(usage)
        self.elapsed_seconds = elapsed_seconds


@dataclass(frozen=True)
class FaultBinding:
    fault_id: str
    scenario_id: str
    legacy_scenario_id: str
    scenario_kind: Literal["legacy", "v4"]
    preset: str
    max_simulation_seconds: float
    max_steps: int


@dataclass(frozen=True)
class EpisodeSpec:
    unit_id: str
    pair_id: str
    fault_id: str
    scenario_id: str
    legacy_scenario_id: str
    scenario_kind: Literal["legacy", "v4"]
    seed: int
    variant: Literal["clean", "fault", "pilot"]
    preset: str
    max_simulation_seconds: float
    max_steps: int
    driver: str = "replay"


@dataclass(frozen=True)
class AutonomousEpisodeSpec(EpisodeSpec):
    driver: str = "llm"
    model: str = STEERING_MODEL
    goal: str = NEUTRAL_REACHABILITY_GOAL
    source_tools_enabled: bool = False


@dataclass(frozen=True)
class EpisodePair:
    pair_id: str
    clean: EpisodeSpec
    fault: EpisodeSpec


@dataclass(frozen=True)
class TrackBSchedule:
    pilots: tuple[EpisodeSpec, ...]
    official_pairs: tuple[EpisodePair, ...]
    autonomous_pairs: tuple[EpisodePair, ...]


@dataclass(frozen=True)
class EpisodeResult:
    transitions: Sequence[dict[str, Any]]
    execution_status: str = "completed"
    replay_divergence_index: int | None = None
    coverage_override: str | None = None
    oracle_override: str | None = None
    divergence_evidence: dict[str, Any] | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "transitions": list(self.transitions),
            "execution_status": self.execution_status,
            "replay_divergence_index": self.replay_divergence_index,
            "coverage_override": self.coverage_override,
            "oracle_override": self.oracle_override,
            "divergence_evidence": self.divergence_evidence,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EpisodeResult":
        transitions = value.get("transitions") or []
        if not isinstance(transitions, list) or any(not isinstance(item, dict) for item in transitions):
            raise CampaignContractError("episode result transitions must be a list of objects")
        divergence = value.get("replay_divergence_index")
        return cls(
            transitions=transitions,
            execution_status=str(value.get("execution_status") or "error"),
            replay_divergence_index=int(divergence) if isinstance(divergence, int) else None,
            coverage_override=(
                str(value["coverage_override"]) if value.get("coverage_override") else None
            ),
            oracle_override=str(value["oracle_override"]) if value.get("oracle_override") else None,
            divergence_evidence=(
                dict(value["divergence_evidence"])
                if isinstance(value.get("divergence_evidence"), dict)
                else None
            ),
            error=str(value.get("error") or ""),
        )


@dataclass(frozen=True)
class InspectionResponse:
    raw_response: Any
    usage: Mapping[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


class CampaignBackend(Protocol):
    def run_pilot(self, spec: EpisodeSpec, output_dir: Path) -> EpisodeResult: ...

    def run_replay(
        self,
        spec: EpisodeSpec,
        replay: dict[str, Any],
        output_dir: Path,
    ) -> EpisodeResult: ...

    def run_autonomous(
        self,
        spec: AutonomousEpisodeSpec,
        output_dir: Path,
    ) -> EpisodeResult: ...


class InspectorAdapter(Protocol):
    def inspect(self, system_prompt: str, payload: dict[str, Any]) -> InspectionResponse: ...


@dataclass(frozen=True)
class BenchmarkCampaignConfig:
    build: Path
    project_root: Path
    output: Path
    headless: bool = False
    quiet: bool = False
    api_url: str | None = None


@dataclass(frozen=True)
class CampaignResult:
    manifest_path: Path
    pairs: tuple[PairScore, ...]
    resumed: bool


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hash_path(path: Path) -> str:
    """Hash a file or build bundle deterministically without retaining file contents."""

    source = path.resolve()
    if source.is_file():
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    if not source.is_dir():
        raise CampaignContractError(f"build does not exist: {source}")
    digest = hashlib.sha256()
    for candidate in sorted(item for item in source.rglob("*") if item.is_file()):
        digest.update(candidate.relative_to(source).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with candidate.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def _file_hash(path: Path) -> str:
    return hash_path(path) if path.exists() else _sha256("missing")


def load_fault_bindings(project_root: Path) -> tuple[FaultBinding, ...]:
    """Join the legacy and v4 private registries into the 11 fixed fault bindings."""

    legacy = {
        scenario.ground_truth.fault_id: FaultBinding(
            fault_id=str(scenario.ground_truth.fault_id),
            scenario_id=scenario.id,
            legacy_scenario_id=scenario.id,
            scenario_kind="legacy",
            preset=scenario.preset,
            max_simulation_seconds=scenario.limits.max_simulation_seconds,
            max_steps=scenario.limits.max_steps,
        )
        for scenario in load_scenarios(project_root / "config" / "qa-scenarios.json")
        if scenario.ground_truth.fault_id
    }
    ground_truth = load_v4_ground_truth(project_root / "config" / "qa-ground-truth-v4.json")
    v4 = {
        str(ground_truth[scenario.id]["fault_id"]): FaultBinding(
            fault_id=str(ground_truth[scenario.id]["fault_id"]),
            scenario_id=scenario.id,
            legacy_scenario_id=scenario.legacy_scenario_id,
            scenario_kind="v4",
            preset=scenario.preset,
            max_simulation_seconds=scenario.limits.max_simulation_seconds,
            max_steps=scenario.limits.max_steps,
        )
        for scenario in load_v4_scenarios(project_root / "config" / "qa-scenarios-v4.json")
        if (ground_truth.get(scenario.id) or {}).get("fault_id")
    }
    joined = {**legacy, **v4}
    missing = [fault_id for fault_id in FAULT_IDS if fault_id not in joined]
    extras = sorted(set(joined) - set(FAULT_IDS))
    if missing or extras:
        raise CampaignContractError(
            f"fault binding registry mismatch: missing={missing}, extras={extras}"
        )
    return tuple(joined[fault_id] for fault_id in FAULT_IDS)


def _episode_spec(
    binding: FaultBinding,
    *,
    mode: Literal["official", "autonomous"],
    seed: int,
    variant: Literal["clean", "fault"],
) -> EpisodeSpec:
    pair_id = f"{mode}/{binding.fault_id}/{seed}"
    values = {
        "unit_id": f"{pair_id}/{variant}",
        "pair_id": pair_id,
        "fault_id": binding.fault_id,
        "scenario_id": binding.scenario_id,
        "legacy_scenario_id": binding.legacy_scenario_id,
        "scenario_kind": binding.scenario_kind,
        "seed": seed,
        "variant": variant,
        "preset": binding.preset,
        "max_simulation_seconds": binding.max_simulation_seconds,
        "max_steps": binding.max_steps,
    }
    if mode == "autonomous":
        return AutonomousEpisodeSpec(**values)
    return EpisodeSpec(**values)


def build_track_b_schedule(bindings: Sequence[FaultBinding]) -> TrackBSchedule:
    """Return the fixed 33-pilot, 66-replay, and 22-autonomous schedule."""

    if tuple(binding.fault_id for binding in bindings) != tuple(FAULT_IDS):
        raise CampaignContractError("Track B bindings must follow the complete fixed fault order")
    pilots: list[EpisodeSpec] = []
    official: list[EpisodePair] = []
    autonomous: list[EpisodePair] = []
    for binding in bindings:
        for seed in SEEDS:
            pair_id = f"official/{binding.fault_id}/{seed}"
            pilots.append(
                EpisodeSpec(
                    unit_id=f"pilot/{binding.fault_id}/{seed}",
                    pair_id=pair_id,
                    fault_id=binding.fault_id,
                    scenario_id=binding.scenario_id,
                    legacy_scenario_id=binding.legacy_scenario_id,
                    scenario_kind=binding.scenario_kind,
                    seed=seed,
                    variant="pilot",
                    preset=binding.preset,
                    max_simulation_seconds=binding.max_simulation_seconds,
                    max_steps=binding.max_steps,
                    driver="heuristic",
                )
            )
            official.append(
                EpisodePair(
                    pair_id,
                    _episode_spec(binding, mode="official", seed=seed, variant="clean"),
                    _episode_spec(binding, mode="official", seed=seed, variant="fault"),
                )
            )
        pair_id = f"autonomous/{binding.fault_id}/{AUTONOMOUS_SEED}"
        autonomous.append(
            EpisodePair(
                pair_id,
                _episode_spec(
                    binding,
                    mode="autonomous",
                    seed=AUTONOMOUS_SEED,
                    variant="clean",
                ),
                _episode_spec(
                    binding,
                    mode="autonomous",
                    seed=AUTONOMOUS_SEED,
                    variant="fault",
                ),
            )
        )
    return TrackBSchedule(tuple(pilots), tuple(official), tuple(autonomous))


def _semantic_selection(action: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = {
        "start_game": "character",
        "select_upgrade": "upgrade",
        "use_item": "item",
    }.get(action)
    if kind is None:
        return None
    try:
        index = int(arguments.get("index", 0))
    except (TypeError, ValueError):
        index = 0
    return {"kind": kind, "index": index}


def _command_digest(commands: Sequence[Mapping[str, Any]]) -> str:
    return _sha256(list(commands))


def _replay_digest(replay: Mapping[str, Any]) -> str:
    return _sha256({key: value for key, value in replay.items() if key != "replay_digest"})


def build_action_replay(
    *,
    replay_id: str,
    scenario_id: str,
    seed: int,
    build_hash: str,
    transitions: Sequence[dict[str, Any]],
    target_command_index: int | None,
) -> dict[str, Any]:
    """Create a replay artifact from the actual deterministic pilot decisions."""

    commands: list[dict[str, Any]] = []
    previous_phase: str | None = None
    for transition in transitions:
        decision = transition.get("decision") or {}
        if not isinstance(decision, dict) or decision.get("tool", "game") != "game":
            continue
        action = str(decision.get("action") or "")
        if not action:
            continue
        arguments = decision.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        observation = transition.get("observation") or {}
        current_phase = observation_phase(observation) if isinstance(observation, dict) else "unknown"
        duration = arguments.get("duration", 0.0)
        try:
            duration_value = float(duration)
        except (TypeError, ValueError):
            duration_value = 0.0
        commands.append(
            {
                "sequence": len(commands),
                "action": action,
                "arguments": dict(arguments),
                "duration_seconds": duration_value,
                "semantic_selection": _semantic_selection(action, arguments),
                "expected_phase_before": previous_phase,
                "expected_phase_after": current_phase,
            }
        )
        previous_phase = current_phase
    replay = {
        "schema_version": ACTION_REPLAY_SCHEMA,
        "replay_id": replay_id,
        "scenario_id": scenario_id,
        "seed": seed,
        "build_hash": build_hash,
        "bootstrap_command": {"action": "observe", "arguments": {}},
        "target_command_index": target_command_index,
        "commands": commands,
        "command_digest": _command_digest(commands),
    }
    replay["replay_digest"] = _replay_digest(replay)
    validate_action_replay(replay, expected_build_hash=build_hash)
    return replay


def validate_action_replay(
    replay: Mapping[str, Any], *, expected_build_hash: str | None = None
) -> None:
    """Reject malformed, cross-build, or modified replay artifacts."""

    if replay.get("schema_version") != ACTION_REPLAY_SCHEMA:
        raise CampaignContractError(f"replay schema must be {ACTION_REPLAY_SCHEMA}")
    if not isinstance(replay.get("seed"), int) or not isinstance(replay.get("scenario_id"), str):
        raise CampaignContractError("replay seed and scenario_id are required")
    build_hash = replay.get("build_hash")
    if not isinstance(build_hash, str) or not build_hash:
        raise CampaignContractError("replay build_hash is required")
    if expected_build_hash is not None and build_hash != expected_build_hash:
        raise CampaignContractError("replay build hash mismatch")
    commands = replay.get("commands")
    if not isinstance(commands, list) or any(not isinstance(item, dict) for item in commands):
        raise CampaignContractError("replay commands must be a list of objects")
    for index, command in enumerate(commands):
        if command.get("sequence") != index or not isinstance(command.get("action"), str):
            raise CampaignContractError("replay command sequence is invalid")
        if not isinstance(command.get("arguments"), dict):
            raise CampaignContractError("replay command arguments must be an object")
    if replay.get("bootstrap_command") != {"action": "observe", "arguments": {}}:
        raise CampaignContractError("replay bootstrap command must be a plain observation")
    if replay.get("command_digest") != _command_digest(commands):
        raise CampaignContractError("replay command digest mismatch")
    if replay.get("replay_digest") != _replay_digest(replay):
        raise CampaignContractError("replay artifact digest mismatch")


def apply_replay_divergence(
    result: EpisodeResult, replay: Mapping[str, Any]
) -> EpisodeResult:
    """Downgrade only pre-target divergence while retaining later divergence as evidence."""

    index = result.replay_divergence_index
    if index is None:
        return result
    target = replay.get("target_command_index")
    pre_target = target is None or index < int(target)
    evidence = {
        "classification": "pre_target" if pre_target else "post_target",
        "command_index": index,
        "target_command_index": target,
    }
    if pre_target:
        return replace(
            result,
            coverage_override="not_reached",
            oracle_override="not_evaluated",
            divergence_evidence=evidence,
        )
    return replace(result, divergence_evidence=evidence)


class CheckpointStore:
    """Persist exact-hash unit state so completed external work can be resumed safely."""

    def __init__(self, path: Path, campaign_hash: str) -> None:
        self.path = path
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise CampaignContractError(f"invalid checkpoint: {error}") from error
            if not isinstance(payload, dict) or payload.get("schema_version") != CHECKPOINT_SCHEMA:
                raise CampaignContractError("invalid campaign checkpoint schema")
            if payload.get("campaign_hash") != campaign_hash:
                raise CampaignContractError("campaign hash mismatch; refusing to reuse output")
            self.payload = payload
        else:
            self.payload = {
                "schema_version": CHECKPOINT_SCHEMA,
                "campaign_hash": campaign_hash,
                "units": {},
                "inspection_usage": {"logical_calls": 0, "http_attempts": 0},
            }
            self._write()

    @property
    def units(self) -> dict[str, Any]:
        units = self.payload.setdefault("units", {})
        if not isinstance(units, dict):
            raise CampaignContractError("checkpoint units must be an object")
        return units

    @property
    def logical_calls(self) -> int:
        return int((self.payload.get("inspection_usage") or {}).get("logical_calls", 0) or 0)

    @property
    def http_attempts(self) -> int:
        return int((self.payload.get("inspection_usage") or {}).get("http_attempts", 0) or 0)

    def reusable(self, unit_id: str, input_hash: str, artifacts: Sequence[Path]) -> bool:
        unit = self.units.get(unit_id)
        if unit is None:
            return False
        if unit.get("input_hash") != input_hash:
            raise CampaignContractError(f"checkpoint input hash mismatch for {unit_id}")
        return unit.get("status") == "complete" and all(path.is_file() for path in artifacts)

    def mark_started(self, unit_id: str, input_hash: str) -> None:
        existing = self.units.get(unit_id)
        if existing is not None and existing.get("input_hash") != input_hash:
            raise CampaignContractError(f"checkpoint input hash mismatch for {unit_id}")
        self.units[unit_id] = {"status": "started", "input_hash": input_hash, "artifacts": []}
        self._write()

    def mark_complete(self, unit_id: str, input_hash: str, artifacts: Sequence[Path]) -> None:
        self.units[unit_id] = {
            "status": "complete",
            "input_hash": input_hash,
            "artifacts": [str(path) for path in artifacts],
        }
        self._write()

    def mark_failed(self, unit_id: str, input_hash: str, error: str) -> None:
        existing = self.units.get(unit_id)
        if existing is not None and existing.get("input_hash") != input_hash:
            raise CampaignContractError(f"checkpoint input hash mismatch for {unit_id}")
        self.units[unit_id] = {
            "status": "failed",
            "input_hash": input_hash,
            "artifacts": [],
            "error": str(error)[:1000],
        }
        self._write()

    def record_logical_call(self) -> None:
        usage = self.payload.setdefault("inspection_usage", {})
        usage["logical_calls"] = int(usage.get("logical_calls", 0) or 0) + 1
        self._write()

    def record_http_attempts(self, attempts: int) -> None:
        usage = self.payload.setdefault("inspection_usage", {})
        usage["http_attempts"] = int(usage.get("http_attempts", 0) or 0) + attempts
        self._write()

    def _write(self) -> None:
        _atomic_write_json(self.path, self.payload)


@dataclass
class InspectionCallBudget:
    cap: int = LOGICAL_INSPECTION_CALL_CAP
    logical_calls: int = 0
    http_attempts: int = 0

    def begin_call(self, checkpoint: CheckpointStore) -> None:
        if self.logical_calls >= self.cap:
            raise CampaignContractError(
                f"logical inspection-call cap exceeded ({self.cap})"
            )
        self.logical_calls += 1
        checkpoint.record_logical_call()

    def finish_call(self, attempts: int, checkpoint: CheckpointStore) -> None:
        self.http_attempts += attempts
        checkpoint.record_http_attempts(attempts)
        if attempts < 1 or attempts > MAX_HTTP_ATTEMPTS:
            raise CampaignContractError("one logical inspection call accepts at most three HTTP attempts")


def inspect_trace_pass(
    *,
    trace_id: str,
    pass_index: int,
    transitions: Sequence[dict[str, Any]],
    output_dir: Path,
    inspector: InspectorAdapter,
    checkpoint: CheckpointStore,
    budget: InspectionCallBudget,
    input_hash: str,
) -> dict[str, Any]:
    """Inspect all generic chunks in one independent pass with chunk-level checkpoints."""

    if pass_index not in (1, 2, 3):
        raise CampaignContractError("inspection pass index must be 1, 2, or 3")
    chunks = build_inspection_chunks(list(transitions))
    pass_dir = output_dir / f"pass-{pass_index}"
    pass_path = pass_dir / "inspection.json"
    pass_unit = f"inspection/{trace_id}/pass-{pass_index}"
    pass_hash = _sha256(
        {
            "input_hash": input_hash,
            "pass_index": pass_index,
            "model": INSPECTOR_MODEL,
            "effort": INSPECTOR_EFFORT,
            "prompt": _sha256(INSPECTOR_SYSTEM_PROMPT),
            "chunks": [_sha256(chunk) for chunk in chunks],
        }
    )
    if checkpoint.reusable(pass_unit, pass_hash, [pass_path]):
        return _read_json_object(pass_path)
    checkpoint.mark_started(pass_unit, pass_hash)
    findings: list[dict[str, Any]] = []
    try:
        for chunk in chunks:
            chunk_id = str(chunk["chunk_id"])
            chunk_path = pass_dir / f"{chunk_id}.audit.json"
            chunk_unit = f"{pass_unit}/{chunk_id}"
            chunk_hash = _sha256(
                {
                    "pass_hash": pass_hash,
                    "chunk": chunk,
                    "source_tools_enabled": False,
                }
            )
            if checkpoint.reusable(chunk_unit, chunk_hash, [chunk_path]):
                audit = _read_json_object(chunk_path)
                normalized = audit.get("normalized") or {}
                findings.extend(normalize_inspection_artifact(normalized)["findings"])
                continue
            checkpoint.mark_started(chunk_unit, chunk_hash)
            budget.begin_call(checkpoint)
            started = time.monotonic()
            try:
                response = inspector.inspect(INSPECTOR_SYSTEM_PROMPT, chunk)
                elapsed = max(response.elapsed_seconds, time.monotonic() - started)
                usage = {str(key): int(value) for key, value in response.usage.items()}
                attempts = int(usage.get("llm_http_attempts", 1) or 1)
                budget.finish_call(attempts, checkpoint)
                retry_wait_ms = int(usage.get("llm_retry_wait_ms", 0) or 0)
                if retry_wait_ms > int(MAX_RETRY_SLEEP_SECONDS * 1000):
                    raise CampaignContractError("inspection retry sleep budget exceeded")
                normalized = normalize_inspection_artifact(response.raw_response)
                audit = {
                    "schema_version": INSPECTION_AUDIT_SCHEMA,
                    "opaque_trace_id": trace_id,
                    "pass_index": pass_index,
                    "chunk_id": chunk_id,
                    "request": {
                        "system_prompt": INSPECTOR_SYSTEM_PROMPT,
                        "payload": chunk,
                        "source_tools_enabled": False,
                    },
                    "raw_response": response.raw_response,
                    "normalized": normalized,
                    "usage": usage,
                    "elapsed_seconds": elapsed,
                    "hashes": {
                        "input": input_hash,
                        "chunk": _sha256(chunk),
                        "model": _sha256(INSPECTOR_MODEL),
                        "prompt": _sha256(INSPECTOR_SYSTEM_PROMPT),
                    },
                }
                _atomic_write_json(chunk_path, audit)
                checkpoint.mark_complete(chunk_unit, chunk_hash, [chunk_path])
                findings.extend(normalized["findings"])
            except InspectionCallError as error:
                usage = {str(key): int(value) for key, value in error.usage.items()}
                attempts = int(usage.get("llm_http_attempts", 1) or 1)
                budget.finish_call(attempts, checkpoint)
                _atomic_write_json(
                    chunk_path,
                    {
                        "schema_version": INSPECTION_AUDIT_SCHEMA,
                        "status": "error",
                        "opaque_trace_id": trace_id,
                        "pass_index": pass_index,
                        "chunk_id": chunk_id,
                        "request": {
                            "system_prompt": INSPECTOR_SYSTEM_PROMPT,
                            "payload": chunk,
                            "source_tools_enabled": False,
                        },
                        "raw_response": None,
                        "normalized": {
                            "schema_version": INSPECTION_SCHEMA_V2,
                            "findings": [],
                        },
                        "usage": usage,
                        "elapsed_seconds": error.elapsed_seconds,
                        "error_type": type(error).__name__,
                        "hashes": {
                            "input": input_hash,
                            "chunk": _sha256(chunk),
                            "model": _sha256(INSPECTOR_MODEL),
                            "prompt": _sha256(INSPECTOR_SYSTEM_PROMPT),
                        },
                    },
                )
                checkpoint.mark_failed(chunk_unit, chunk_hash, type(error).__name__)
                raise
            except Exception as error:
                checkpoint.mark_failed(chunk_unit, chunk_hash, f"{type(error).__name__}: {error}")
                raise
        artifact = normalize_inspection_artifact(
            {"schema_version": INSPECTION_SCHEMA_V2, "findings": findings}
        )
        _atomic_write_json(pass_path, artifact)
        checkpoint.mark_complete(pass_unit, pass_hash, [pass_path])
        return artifact
    except Exception as error:
        detail = (
            type(error).__name__
            if isinstance(error, InspectionCallError)
            else f"{type(error).__name__}: {error}"
        )
        checkpoint.mark_failed(pass_unit, pass_hash, detail)
        raise


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignContractError(f"invalid JSON artifact {path}: {error}") from error
    if not isinstance(value, dict):
        raise CampaignContractError(f"JSON artifact must contain an object: {path}")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _campaign_hash(config: BenchmarkCampaignConfig, build_hash: str) -> str:
    root = config.project_root.resolve()
    return _sha256(
        {
            "build_hash": build_hash,
            "build_path": str(config.build.resolve()),
            "project_root": str(root),
            "api_url_hash": _sha256(config.api_url or "default"),
            "headless": config.headless,
            "quiet": config.quiet,
            "steering_model": STEERING_MODEL,
            "inspector_model": INSPECTOR_MODEL,
            "inspector_effort": INSPECTOR_EFFORT,
            "inspector_prompt_hash": _sha256(INSPECTOR_SYSTEM_PROMPT),
            "rubric_hash": _file_hash(root / "config" / "qa-detection-rubric.json"),
            "legacy_scenarios_hash": _file_hash(root / "config" / "qa-scenarios.json"),
            "v4_scenarios_hash": _file_hash(root / "config" / "qa-scenarios-v4.json"),
            "v4_ground_truth_hash": _file_hash(root / "config" / "qa-ground-truth-v4.json"),
            "seeds": SEEDS,
            "autonomous_seed": AUTONOMOUS_SEED,
            "inspection_repetitions": INSPECTION_REPETITIONS,
            "logical_call_cap": LOGICAL_INSPECTION_CALL_CAP,
        }
    )


def _binding_by_fault(bindings: Sequence[FaultBinding]) -> dict[str, FaultBinding]:
    return {binding.fault_id: binding for binding in bindings}


def _evaluate_binding(
    binding: FaultBinding,
    project_root: Path,
    transitions: Sequence[dict[str, Any]],
) -> tuple[str, str]:
    rows = list(transitions)
    if not rows:
        return "not_reached", "not_evaluated"
    if binding.scenario_kind == "legacy":
        scenario = next(
            item
            for item in load_scenarios(project_root / "config" / "qa-scenarios.json")
            if item.id == binding.scenario_id
        )
        coverage = evaluate_coverage(scenario, rows)
        oracle = evaluate_oracle(scenario, rows)
        return coverage.status, oracle.verdict
    scenario = next(
        item
        for item in load_v4_scenarios(project_root / "config" / "qa-scenarios-v4.json")
        if item.id == binding.scenario_id
    )
    oracle = evaluate_v4_oracle(scenario.oracle.id, rows)
    return ("reached" if oracle.verdict != "not_evaluated" else "not_reached"), oracle.verdict


def _target_command_index(
    binding: FaultBinding,
    project_root: Path,
    transitions: Sequence[dict[str, Any]],
) -> int | None:
    for index in range(len(transitions)):
        try:
            coverage, _ = _evaluate_binding(binding, project_root, transitions[: index + 1])
        except (ValueError, TypeError, KeyError):
            continue
        if coverage == "reached":
            return index
    return None


def _unit_hash(
    *,
    campaign_hash: str,
    spec: EpisodeSpec,
    replay_digest: str | None = None,
) -> str:
    return _sha256(
        {
            "campaign_hash": campaign_hash,
            "spec": asdict(spec),
            "replay_digest": replay_digest,
        }
    )


def _run_episode_unit(
    *,
    checkpoint: CheckpointStore,
    unit_id: str,
    input_hash: str,
    path: Path,
    execute: Callable[[], EpisodeResult],
) -> tuple[EpisodeResult, bool]:
    if checkpoint.reusable(unit_id, input_hash, [path]):
        return EpisodeResult.from_dict(_read_json_object(path)), True
    checkpoint.mark_started(unit_id, input_hash)
    try:
        result = execute()
        _atomic_write_json(path, result.to_dict())
        if result.execution_status == "completed":
            checkpoint.mark_complete(unit_id, input_hash, [path])
        else:
            checkpoint.mark_failed(
                unit_id,
                input_hash,
                result.error or f"execution_status={result.execution_status}",
            )
        return result, False
    except Exception as error:
        checkpoint.mark_failed(unit_id, input_hash, f"{type(error).__name__}: {error}")
        raise


def _run_pilot_unit(
    *,
    config: BenchmarkCampaignConfig,
    build_hash: str,
    campaign_hash: str,
    binding: FaultBinding,
    spec: EpisodeSpec,
    backend: CampaignBackend,
    checkpoint: CheckpointStore,
) -> tuple[dict[str, Any], bool]:
    directory = config.output / "pilots" / spec.fault_id / str(spec.seed)
    episode_path = directory / "episode-result.json"
    replay_path = directory / "action-replay.json"
    unit_hash = _unit_hash(campaign_hash=campaign_hash, spec=spec)
    if checkpoint.reusable(spec.unit_id, unit_hash, [episode_path, replay_path]):
        replay = _read_json_object(replay_path)
        validate_action_replay(replay, expected_build_hash=build_hash)
        EpisodeResult.from_dict(_read_json_object(episode_path))
        return replay, True
    checkpoint.mark_started(spec.unit_id, unit_hash)
    try:
        result = backend.run_pilot(spec, directory)
        target_index = _target_command_index(
            binding, config.project_root, result.transitions
        )
        replay = build_action_replay(
            replay_id=spec.unit_id.replace("/", "-"),
            scenario_id=spec.scenario_id,
            seed=spec.seed,
            build_hash=build_hash,
            transitions=result.transitions,
            target_command_index=target_index,
        )
        _atomic_write_json(episode_path, result.to_dict())
        _atomic_write_json(replay_path, replay)
        if result.execution_status == "completed":
            checkpoint.mark_complete(spec.unit_id, unit_hash, [episode_path, replay_path])
        else:
            checkpoint.mark_failed(
                spec.unit_id,
                unit_hash,
                result.error or f"execution_status={result.execution_status}",
            )
        return replay, False
    except Exception as error:
        checkpoint.mark_failed(spec.unit_id, unit_hash, f"{type(error).__name__}: {error}")
        raise


def _inspect_episode(
    *,
    opaque_trace_id: str,
    result: EpisodeResult,
    output: Path,
    inspector: InspectorAdapter,
    checkpoint: CheckpointStore,
    budget: InspectionCallBudget,
    campaign_hash: str,
) -> list[dict[str, Any] | None]:
    input_hash = _sha256(
        {
            "campaign_hash": campaign_hash,
            "opaque_trace_id": opaque_trace_id,
            "transitions": list(result.transitions),
        }
    )
    passes: list[dict[str, Any] | None] = []
    for pass_index in range(1, INSPECTION_REPETITIONS + 1):
        try:
            passes.append(
                inspect_trace_pass(
                    trace_id=opaque_trace_id,
                    pass_index=pass_index,
                    transitions=result.transitions,
                    output_dir=output / "inspections" / opaque_trace_id,
                    inspector=inspector,
                    checkpoint=checkpoint,
                    budget=budget,
                    input_hash=input_hash,
                )
            )
        except CampaignContractError:
            raise
        except Exception:
            passes.append(None)
    return passes


def _score_episode_pair(
    *,
    pair: EpisodePair,
    clean_result: EpisodeResult,
    fault_result: EpisodeResult,
    binding: FaultBinding,
    config: BenchmarkCampaignConfig,
    inspector: InspectorAdapter,
    checkpoint: CheckpointStore,
    budget: InspectionCallBudget,
    campaign_hash: str,
) -> PairScore:
    evaluations: list[TraceEvaluation] = []
    for spec, result in ((pair.clean, clean_result), (pair.fault, fault_result)):
        opaque_id = _sha256({"campaign_hash": campaign_hash, "unit_id": spec.unit_id})[:24]
        inspections = _inspect_episode(
            opaque_trace_id=opaque_id,
            result=result,
            output=config.output,
            inspector=inspector,
            checkpoint=checkpoint,
            budget=budget,
            campaign_hash=campaign_hash,
        )
        try:
            coverage, oracle = _evaluate_binding(
                binding, config.project_root, result.transitions
            )
        except Exception:
            coverage, oracle = "not_reached", "not_evaluated"
        coverage = result.coverage_override or coverage
        oracle = result.oracle_override or oracle
        evaluations.append(
            TraceEvaluation(
                trace_id=opaque_id,
                fault_id=spec.fault_id,
                variant=spec.variant,
                execution_status=result.execution_status,
                coverage_status=coverage,
                oracle_verdict=oracle,
                transitions=result.transitions,
                inspection_passes=inspections,
            )
        )
    return score_pair(pair.pair_id, evaluations[0], evaluations[1])


def _write_reports(
    output: Path,
    official: Sequence[PairScore],
    autonomous: Sequence[PairScore],
    campaign_hash: str,
) -> None:
    groups = {
        "official": official,
        "autonomous": autonomous,
        "combined": [*official, *autonomous],
    }
    for name, pairs in groups.items():
        report = build_benchmark_report(
            pairs,
            metadata={
                "campaign_id": campaign_hash[:16],
                "track": "B",
                "score_surface": name,
            },
        )
        if name == "combined":
            json_path = output / "detection-benchmark.json"
            markdown_path = output / "detection-benchmark.ko.md"
        else:
            json_path = output / "metrics" / f"{name}.json"
            markdown_path = output / "metrics" / f"{name}.ko.md"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(benchmark_report_json(report), encoding="utf-8")
        markdown_path.write_text(render_benchmark_markdown(report), encoding="utf-8")


def run_detection_campaign(
    config: BenchmarkCampaignConfig,
    *,
    backend: CampaignBackend,
    inspector: InspectorAdapter,
) -> CampaignResult:
    """Execute or exactly resume the complete fixed Track B campaign."""

    build_hash = hash_path(config.build)
    campaign_hash = _campaign_hash(config, build_hash)
    output = config.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = CheckpointStore(output / "checkpoint.json", campaign_hash)
    budget = InspectionCallBudget(
        cap=LOGICAL_INSPECTION_CALL_CAP,
        logical_calls=checkpoint.logical_calls,
        http_attempts=checkpoint.http_attempts,
    )
    bindings = load_fault_bindings(config.project_root)
    by_fault = _binding_by_fault(bindings)
    schedule = build_track_b_schedule(bindings)
    manifest_path = output / "campaign-manifest.json"
    resumed = bool(checkpoint.units)
    if manifest_path.exists():
        existing = _read_json_object(manifest_path)
        if existing.get("campaign_hash") != campaign_hash:
            raise CampaignContractError("campaign hash mismatch; refusing to reuse manifest")

    pilots = {spec.pair_id: spec for spec in schedule.pilots}
    official_scores: list[PairScore] = []
    autonomous_scores: list[PairScore] = []
    replay_metadata: list[dict[str, Any]] = []
    trace_metadata: list[dict[str, Any]] = []
    for pair in schedule.official_pairs:
        pilot = pilots[pair.pair_id]
        binding = by_fault[pair.clean.fault_id]
        replay, pilot_resumed = _run_pilot_unit(
            config=replace(config, output=output),
            build_hash=build_hash,
            campaign_hash=campaign_hash,
            binding=binding,
            spec=pilot,
            backend=backend,
            checkpoint=checkpoint,
        )
        validate_action_replay(replay, expected_build_hash=build_hash)
        replay_metadata.append(
            {
                "pair_id": pair.pair_id,
                "pilot_unit_id": pilot.unit_id,
                "command_digest": replay["command_digest"],
                "replay_digest": replay["replay_digest"],
                "pilot_resumed": pilot_resumed,
                "scored": False,
            }
        )
        results: list[EpisodeResult] = []
        for spec in (pair.clean, pair.fault):
            trace_dir = output / "traces" / spec.unit_id
            path = trace_dir / "episode-result.json"
            unit_hash = _unit_hash(
                campaign_hash=campaign_hash,
                spec=spec,
                replay_digest=str(replay["replay_digest"]),
            )
            result, was_resumed = _run_episode_unit(
                checkpoint=checkpoint,
                unit_id=spec.unit_id,
                input_hash=unit_hash,
                path=path,
                execute=lambda spec=spec, trace_dir=trace_dir: backend.run_replay(
                    spec, replay, trace_dir
                ),
            )
            result = apply_replay_divergence(result, replay)
            results.append(result)
            trace_metadata.append(
                {
                    "unit_id": spec.unit_id,
                    "pair_id": spec.pair_id,
                    "variant": spec.variant,
                    "driver": spec.driver,
                    "resumed": was_resumed,
                    "replay_digest": replay["replay_digest"],
                    "divergence": result.divergence_evidence,
                }
            )
        official_scores.append(
            _score_episode_pair(
                pair=pair,
                clean_result=results[0],
                fault_result=results[1],
                binding=binding,
                config=replace(config, output=output),
                inspector=inspector,
                checkpoint=checkpoint,
                budget=budget,
                campaign_hash=campaign_hash,
            )
        )

    for pair in schedule.autonomous_pairs:
        binding = by_fault[pair.clean.fault_id]
        results = []
        for base_spec in (pair.clean, pair.fault):
            if not isinstance(base_spec, AutonomousEpisodeSpec):
                raise CampaignContractError("autonomous schedule must use AutonomousEpisodeSpec")
            trace_dir = output / "traces" / base_spec.unit_id
            path = trace_dir / "episode-result.json"
            unit_hash = _unit_hash(campaign_hash=campaign_hash, spec=base_spec)
            result, was_resumed = _run_episode_unit(
                checkpoint=checkpoint,
                unit_id=base_spec.unit_id,
                input_hash=unit_hash,
                path=path,
                execute=lambda spec=base_spec, trace_dir=trace_dir: backend.run_autonomous(
                    spec, trace_dir
                ),
            )
            results.append(result)
            trace_metadata.append(
                {
                    "unit_id": base_spec.unit_id,
                    "pair_id": base_spec.pair_id,
                    "variant": base_spec.variant,
                    "driver": base_spec.driver,
                    "model": base_spec.model,
                    "neutral_goal": True,
                    "source_tools_enabled": base_spec.source_tools_enabled,
                    "resumed": was_resumed,
                }
            )
        autonomous_scores.append(
            _score_episode_pair(
                pair=pair,
                clean_result=results[0],
                fault_result=results[1],
                binding=binding,
                config=replace(config, output=output),
                inspector=inspector,
                checkpoint=checkpoint,
                budget=budget,
                campaign_hash=campaign_hash,
            )
        )

    _write_reports(output, official_scores, autonomous_scores, campaign_hash)
    manifest = {
        "schema_version": CAMPAIGN_MANIFEST_SCHEMA,
        "campaign_hash": campaign_hash,
        "status": "complete",
        "track": "B",
        "hashes": {
            "build": build_hash,
            "config": campaign_hash,
            "steering_model": _sha256(STEERING_MODEL),
            "inspector_model": _sha256(INSPECTOR_MODEL),
            "inspector_prompt": _sha256(INSPECTOR_SYSTEM_PROMPT),
        },
        "models": {
            "steering": STEERING_MODEL,
            "inspection": INSPECTOR_MODEL,
            "inspection_effort": INSPECTOR_EFFORT,
        },
        "limits": {
            "logical_inspection_call_cap": LOGICAL_INSPECTION_CALL_CAP,
            "planned_cross_track_max": PLANNED_MAX_LOGICAL_INSPECTION_CALLS,
            "http_attempts_per_logical_call": MAX_HTTP_ATTEMPTS,
            "retry_sleep_budget_seconds": MAX_RETRY_SLEEP_SECONDS,
        },
        "counts": {
            "pilots": len(schedule.pilots),
            "official_replay_traces": len(schedule.official_pairs) * 2,
            "autonomous_traces": len(schedule.autonomous_pairs) * 2,
            "scored_pairs": len(official_scores) + len(autonomous_scores),
            "logical_inspection_calls": checkpoint.logical_calls,
            "http_attempts": checkpoint.http_attempts,
        },
        "pilot_replays": replay_metadata,
        "traces": trace_metadata,
        "reports": {
            "combined_json": "detection-benchmark.json",
            "combined_markdown": "detection-benchmark.ko.md",
            "official_json": "metrics/official.json",
            "official_markdown": "metrics/official.ko.md",
            "autonomous_json": "metrics/autonomous.json",
            "autonomous_markdown": "metrics/autonomous.ko.md",
        },
    }
    _atomic_write_json(manifest_path, manifest)
    return CampaignResult(
        manifest_path=manifest_path,
        pairs=tuple([*official_scores, *autonomous_scores]),
        resumed=resumed,
    )


class LLMInspectorAdapter:
    """Call the fixed inspector with bounded transport retries and no source tools."""

    def __init__(self, *, api_url: str | None = None) -> None:
        self.planner = LLMPlanner(
            "qa",
            INSPECTOR_MODEL,
            TestCharter(objective=NEUTRAL_REACHABILITY_GOAL),
            5.0,
            api_url,
            max_attempts=MAX_HTTP_ATTEMPTS,
            retry_budget_seconds=MAX_RETRY_SLEEP_SECONDS,
            reasoning_effort=INSPECTOR_EFFORT,
        )

    def inspect(self, system_prompt: str, payload: dict[str, Any]) -> InspectionResponse:
        started = time.monotonic()
        try:
            response = self.planner._request(
                system_prompt,
                json.dumps(payload, ensure_ascii=False),
                response_schema=FINDINGS_SCHEMA,
                include_planning_history=False,
            )
        except Exception as error:
            usage = self.planner.take_last_usage()
            raise InspectionCallError(
                type(error).__name__,
                usage=usage,
                elapsed_seconds=max(0.0, time.monotonic() - started),
            ) from error
        usage = self.planner.take_last_usage()
        return InspectionResponse(
            raw_response=response,
            usage=usage,
            elapsed_seconds=max(0.0, time.monotonic() - started),
        )


class BridgeCampaignBackend:
    """Execute pilots/autonomous sessions with the existing runner and replay via the bridge."""

    def __init__(
        self,
        config: BenchmarkCampaignConfig,
        *,
        parse_run_arguments: Callable[[list[str]], Any] = parse_run_args,
        run_session_fn: Callable[[Any], int] = run_session,
        adapter_factory: Callable[..., Any] = VampireSurvivorsAdapter,
    ) -> None:
        self.config = config
        self.parse_run_arguments = parse_run_arguments
        self.run_session_fn = run_session_fn
        self.adapter_factory = adapter_factory
        self.build_hash = hash_path(config.build)

    def run_pilot(self, spec: EpisodeSpec, output_dir: Path) -> EpisodeResult:
        arguments = self._common_run_arguments(spec, output_dir)
        arguments.extend(
            [
                "--policy",
                "heuristic",
                "--scenario",
                spec.legacy_scenario_id,
            ]
        )
        args = self.parse_run_arguments(arguments)
        args.max_simulation_seconds = spec.max_simulation_seconds
        args.max_steps = spec.max_steps
        self.run_session_fn(args)
        return self._load_session_result(output_dir)

    def run_autonomous(
        self,
        spec: AutonomousEpisodeSpec,
        output_dir: Path,
    ) -> EpisodeResult:
        if spec.driver != "llm" or spec.model != STEERING_MODEL:
            raise CampaignContractError("Track B autonomous episodes require the fixed pure LLM model")
        if spec.goal != NEUTRAL_REACHABILITY_GOAL or spec.source_tools_enabled:
            raise CampaignContractError("Track B autonomous episodes require the neutral blind charter")
        arguments = self._common_run_arguments(spec, output_dir)
        arguments.extend(
            [
                "--policy",
                "llm",
                "--model",
                STEERING_MODEL,
                "--objective",
                NEUTRAL_REACHABILITY_GOAL,
                "--max-source-steps",
                "0",
                "--max-simulation-seconds",
                str(spec.max_simulation_seconds),
                "--max-steps",
                str(spec.max_steps),
            ]
        )
        if spec.variant == "fault":
            arguments.extend(["--fault", spec.fault_id])
        args = self.parse_run_arguments(arguments)
        self.run_session_fn(args)
        return self._load_session_result(output_dir)

    def run_replay(
        self,
        spec: EpisodeSpec,
        replay: dict[str, Any],
        output_dir: Path,
    ) -> EpisodeResult:
        validate_action_replay(replay, expected_build_hash=self.build_hash)
        if replay.get("scenario_id") != spec.scenario_id or replay.get("seed") != spec.seed:
            raise CampaignContractError("replay scenario or seed does not match the episode")
        output_dir.mkdir(parents=True, exist_ok=True)
        adapter = self.adapter_factory(
            game_exe=self.config.build,
            session_dir=output_dir,
            mode="qa",
            headless=self.config.headless,
            run_id=spec.unit_id.replace("/", "-"),
            scenario_id=spec.scenario_id,
        )
        transitions: list[dict[str, Any]] = []
        divergence_index: int | None = None
        execution_status = "completed"
        error = ""
        try:
            adapter.start(
                seed=spec.seed,
                preset=spec.preset,
                faults=[spec.fault_id] if spec.variant == "fault" else [],
            )
            bootstrap = replay["bootstrap_command"]
            bootstrap_observation = adapter.command(
                bootstrap["action"],
                decision_id=f"{spec.unit_id.replace('/', '-')}-bootstrap",
                **bootstrap["arguments"],
            )
            previous_phase: str | None = observation_phase(bootstrap_observation)
            for index, command in enumerate(replay["commands"]):
                expected_before = command.get("expected_phase_before")
                if (
                    divergence_index is None
                    and previous_phase is not None
                    and expected_before is not None
                    and previous_phase != expected_before
                ):
                    divergence_index = index
                decision = {
                    "tool": "game",
                    "action": command["action"],
                    "arguments": dict(command["arguments"]),
                    "decision_id": f"{spec.unit_id.replace('/', '-')}-{index:08d}",
                }
                observed = execute_game_action(adapter, decision)
                current_phase = observation_phase(observed)
                expected_after = command.get("expected_phase_after")
                if (
                    divergence_index is None
                    and expected_after is not None
                    and current_phase != expected_after
                ):
                    divergence_index = index
                transitions.append(
                    {
                        "step": index,
                        "decision": decision,
                        "observation": observed,
                    }
                )
                previous_phase = current_phase
        except Exception as caught:
            execution_status = "infrastructure_error"
            error = f"{type(caught).__name__}: {caught}"
        finally:
            episode_exit = adapter.stop()
            if getattr(episode_exit, "kind", "normal") != "normal":
                execution_status = "infrastructure_error"
                error = str(getattr(episode_exit, "detail", "") or episode_exit.kind)
        steps_path = output_dir / "steps.jsonl"
        steps_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in transitions),
            encoding="utf-8",
        )
        return EpisodeResult(
            transitions=transitions,
            execution_status=execution_status,
            replay_divergence_index=divergence_index,
            error=error,
        )

    def _common_run_arguments(self, spec: EpisodeSpec, output_dir: Path) -> list[str]:
        arguments = [
            "--game-exe",
            str(self.config.build),
            "--project-root",
            str(self.config.project_root),
            "--output",
            str(output_dir),
            "--mode",
            "qa",
            "--bridge-scenario-id",
            spec.scenario_id,
            "--seed",
            str(spec.seed),
        ]
        if self.config.api_url:
            arguments.extend(["--api-url", self.config.api_url])
        if self.config.headless:
            arguments.append("--headless")
        if self.config.quiet:
            arguments.append("--quiet")
        return arguments

    @staticmethod
    def _load_session_result(output_dir: Path) -> EpisodeResult:
        steps_path = output_dir / "steps.jsonl"
        verdict_path = output_dir / "verdict.json"
        try:
            transitions = [
                json.loads(line)
                for line in steps_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            verdict = _read_json_object(verdict_path)
        except (CampaignContractError, OSError, json.JSONDecodeError) as error:
            return EpisodeResult(
                transitions=[],
                execution_status="infrastructure_error",
                error=f"invalid session artifact: {error}",
            )
        if any(not isinstance(item, dict) for item in transitions):
            return EpisodeResult(
                transitions=[],
                execution_status="contract_error",
                error="steps.jsonl must contain JSON objects",
            )
        return EpisodeResult(
            transitions=transitions,
            execution_status=str(verdict.get("execution_status") or "infrastructure_error"),
        )
