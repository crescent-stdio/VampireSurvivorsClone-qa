from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .run import parse_args as parse_run_args
from .run import run_session
from .scenarios import (
    Scenario,
    ScenarioContractError,
    load_scenario,
    load_scenarios,
    scenario_fingerprint,
)


DETERMINISTIC_GATE_SCENARIO_IDS = (
    "easy-health-ratio",
    "easy-relative-position",
    "medium-upgrade-effect",
    "medium-chest-transition",
    "hard-experience-drift",
    "hard-restart-currency",
    "control-valid-observation",
    "control-normal-transitions",
    "control-long-progression",
)


@dataclass(frozen=True)
class RunResult:
    scenario_id: str
    seed: int
    output_dir: Path
    return_code: int


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioContractError(f"invalid benchmark artifact {path}: {error}") from error
    if not isinstance(value, dict):
        raise ScenarioContractError(f"benchmark artifact must be an object: {path}")
    return value


def _read_steps(output_dir: Path) -> list[dict[str, Any]]:
    path = output_dir / "steps.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        steps = [json.loads(line) for line in lines if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioContractError(f"invalid benchmark artifact {path}: {error}") from error
    if any(not isinstance(step, dict) for step in steps):
        raise ScenarioContractError(f"benchmark steps must be JSON objects: {path}")
    return steps


def _without_volatile_identifiers(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_volatile_identifiers(item)
            for key, item in value.items()
            if key != "id" and not key.endswith("_id")
        }
    if isinstance(value, list):
        return [_without_volatile_identifiers(item) for item in value]
    return value


def transition_sequence(output_dir: Path) -> list[dict[str, Any]]:
    sequence: list[dict[str, Any]] = []
    for step in _read_steps(output_dir):
        decision = step.get("decision") or {}
        observation = step.get("observation") or {}
        sequence.append(
            _without_volatile_identifiers(
                {
                    "decision": {
                        "action": decision.get("action") or "",
                        "arguments": decision.get("arguments") or {},
                    },
                    "observation": {
                        key: observation.get(key)
                        for key in (
                            "ok",
                            "result",
                            "mode",
                            "seed",
                            "scene",
                            "phase",
                            "paused",
                            "pause_reason",
                            "awaiting_agent_command",
                            "time_scale",
                            "player",
                            "world",
                            "progress",
                            "menu",
                            "inventory",
                            "controller",
                            "event_state",
                            "available_actions",
                        )
                        if key in observation
                    },
                }
            )
        )
    return sequence


def _available_evidence_refs(steps: Sequence[dict[str, Any]]) -> set[str]:
    references: set[str] = set()
    for step in steps:
        observation = step.get("observation") or {}
        observation_id = str(observation.get("observation_id") or "")
        event_id = str((observation.get("event_state") or {}).get("event_id") or "")
        if observation_id:
            references.add(observation_id)
        if event_id:
            references.add(event_id)
    return references


def verify_deterministic_gate(
    scenarios: Sequence[Scenario],
    results: Sequence[RunResult],
) -> dict[str, Any]:
    scenario_by_id = {scenario.id: scenario for scenario in scenarios}
    result_by_id = {result.scenario_id: result for result in results}
    if len(scenario_by_id) != len(scenarios) or len(result_by_id) != len(results):
        raise ScenarioContractError("deterministic gate requires one run per unique scenario")
    if set(scenario_by_id) != set(result_by_id):
        raise ScenarioContractError("deterministic gate scenario and result identifiers differ")
    if set(scenario_by_id) != set(DETERMINISTIC_GATE_SCENARIO_IDS) or any(
        scenario.ground_truth.review_status != "approved" for scenario in scenarios
    ):
        raise ScenarioContractError(
            "deterministic gate requires the exact approved scenario set"
        )
    if len({result.seed for result in results}) != 1:
        raise ScenarioContractError("deterministic gate requires one shared seed")

    faults_detected = 0
    controls_passed = 0
    control_false_positives = 0
    run_summaries: list[dict[str, Any]] = []
    for scenario_id, scenario in scenario_by_id.items():
        result = result_by_id[scenario_id]
        if result.return_code != 0:
            raise ScenarioContractError(
                f"scenario {scenario_id} returned non-zero exit code {result.return_code}"
            )
        manifest = _read_json(result.output_dir / "manifest.json")
        verdict = _read_json(result.output_dir / "verdict.json")
        steps = _read_steps(result.output_dir)
        if manifest.get("scenario_id") != scenario_id or manifest.get("seed") != result.seed:
            raise ScenarioContractError(f"scenario {scenario_id} manifest identity mismatch")
        if manifest.get("scenario_fingerprint") != scenario_fingerprint(scenario):
            raise ScenarioContractError(f"scenario {scenario_id} manifest fingerprint mismatch")
        if manifest.get("preset") != scenario.preset:
            raise ScenarioContractError(f"scenario {scenario_id} manifest preset mismatch")
        if manifest.get("mode") != "qa" or manifest.get("policy") != "heuristic":
            raise ScenarioContractError(
                f"scenario {scenario_id} is not a qa heuristic artifact"
            )
        if manifest.get("fault_id") != scenario.ground_truth.fault_id:
            raise ScenarioContractError(f"scenario {scenario_id} manifest fault_id mismatch")
        if verdict.get("execution_status") != "completed":
            raise ScenarioContractError(f"scenario {scenario_id} execution did not complete")
        if verdict.get("coverage_status") != "reached":
            raise ScenarioContractError(f"scenario {scenario_id} coverage was not reached")

        evidence_refs = [str(reference) for reference in verdict.get("evidence_refs") or []]
        missing = sorted(set(evidence_refs) - _available_evidence_refs(steps))
        if not evidence_refs or missing:
            raise ScenarioContractError(
                f"scenario {scenario_id} has unresolvable evidence refs: {missing}"
            )

        agent_channel = (result.output_dir / "steps.jsonl").read_text(encoding="utf-8")
        for request_log in result.output_dir.glob("*request*.jsonl"):
            agent_channel += request_log.read_text(encoding="utf-8")
        if "ground_truth" in agent_channel:
            raise ScenarioContractError(f"scenario {scenario_id} leaked ground truth")
        fault_id = scenario.ground_truth.fault_id
        if fault_id and fault_id in agent_channel:
            raise ScenarioContractError(f"scenario {scenario_id} leaked fault_id")

        oracle_verdict = str(verdict.get("oracle_verdict") or "")
        if fault_id:
            if oracle_verdict != "fail":
                raise ScenarioContractError(f"scenario {scenario_id} fault was not detected")
            faults_detected += 1
        else:
            if oracle_verdict == "fail":
                control_false_positives += 1
            if oracle_verdict != "pass":
                raise ScenarioContractError(f"scenario {scenario_id} control did not pass")
            controls_passed += 1
        run_summaries.append(
            {
                "scenario_id": scenario_id,
                "seed": result.seed,
                "oracle_verdict": oracle_verdict,
                "evidence_refs": evidence_refs,
            }
        )

    if faults_detected != 6 or controls_passed != 3 or control_false_positives != 0:
        raise ScenarioContractError("deterministic gate requires 6/6 faults and 0/3 controls")
    return {
        "result": "pass",
        "faults_detected": faults_detected,
        "controls_passed": controls_passed,
        "control_false_positives": control_false_positives,
        "runs": run_summaries,
    }


def verify_replay(first: RunResult, second: RunResult) -> None:
    if first.scenario_id != second.scenario_id or first.seed != second.seed:
        raise ScenarioContractError("replay comparison requires the same scenario and seed")
    for result in (first, second):
        if result.return_code != 0:
            raise ScenarioContractError(
                f"replay run returned non-zero exit code {result.return_code}"
            )
    first_manifest = _read_json(first.output_dir / "manifest.json")
    second_manifest = _read_json(second.output_dir / "manifest.json")
    required_string_keys = ("scenario_fingerprint", "preset", "mode", "policy")
    for result, manifest in ((first, first_manifest), (second, second_manifest)):
        if manifest.get("scenario_id") != result.scenario_id or manifest.get("seed") != result.seed:
            raise ScenarioContractError("replay manifest identity mismatch")
        if any(
            not isinstance(manifest.get(key), str) or not manifest[key].strip()
            for key in required_string_keys
        ):
            raise ScenarioContractError(
                "replay manifest is missing required configuration"
            )
    identity_keys = (
        "scenario_id",
        "scenario_fingerprint",
        "seed",
        "preset",
        "mode",
        "policy",
        "fault_id",
    )
    if any(first_manifest.get(key) != second_manifest.get(key) for key in identity_keys):
        raise ScenarioContractError("replay manifest configuration mismatch")
    first_verdict = _read_json(first.output_dir / "verdict.json")
    second_verdict = _read_json(second.output_dir / "verdict.json")
    for verdict in (first_verdict, second_verdict):
        if verdict.get("execution_status") != "completed":
            raise ScenarioContractError("replay execution did not complete")
        if verdict.get("coverage_status") != "reached":
            raise ScenarioContractError("replay coverage was not reached")
    if first_verdict.get("oracle_verdict") != second_verdict.get("oracle_verdict"):
        raise ScenarioContractError("replay oracle verdict mismatch")
    if transition_sequence(first.output_dir) != transition_sequence(second.output_dir):
        raise ScenarioContractError("replay transition sequence mismatch")


def validate_deterministic_gate_request(
    scenarios: Sequence[Scenario],
    seeds: Sequence[int] | None,
    *,
    mode: str,
    policy: str,
) -> None:
    scenario_ids = {scenario.id for scenario in scenarios}
    if scenario_ids != set(DETERMINISTIC_GATE_SCENARIO_IDS) or len(scenarios) != len(
        DETERMINISTIC_GATE_SCENARIO_IDS
    ):
        raise ScenarioContractError(
            "deterministic gate requires the exact approved scenario set"
        )
    if any(scenario.ground_truth.review_status != "approved" for scenario in scenarios):
        raise ScenarioContractError("deterministic gate requires approved ground truth")
    if seeds is None or len(seeds) != 1:
        raise ScenarioContractError("deterministic gate requires exactly one explicit seed")
    if mode != "qa" or policy != "heuristic":
        raise ScenarioContractError("deterministic gate requires qa mode and heuristic policy")


def run_benchmark(
    scenarios: Sequence[Scenario],
    seeds: Sequence[int] | None,
    *,
    game_exe: Path,
    project_root: Path,
    output_root: Path,
    mode: str = "qa",
    policy: str = "heuristic",
    model: str = "",
    api_url: str | None = None,
    headless: bool = False,
    quiet: bool = False,
) -> list[RunResult]:
    results: list[RunResult] = []
    requested_seeds = list(seeds) if seeds is not None else None
    for scenario in scenarios:
        scenario_seeds = requested_seeds or scenario.seed_set
        for seed in scenario_seeds:
            selected_seed = scenario.select_seed(seed)
            output_dir = output_root / scenario.id / str(selected_seed)
            arguments = [
                "--game-exe",
                str(game_exe),
                "--project-root",
                str(project_root),
                "--output",
                str(output_dir),
                "--mode",
                mode,
                "--policy",
                policy,
                "--scenario",
                scenario.id,
                "--seed",
                str(selected_seed),
            ]
            if model:
                arguments.extend(["--model", model])
            if api_url:
                arguments.extend(["--api-url", api_url])
            if headless:
                arguments.append("--headless")
            if quiet:
                arguments.append("--quiet")
            return_code = run_session(parse_run_args(arguments))
            results.append(
                RunResult(
                    scenario_id=scenario.id,
                    seed=selected_seed,
                    output_dir=output_dir,
                    return_code=return_code,
                )
            )
    return results


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QA scenarios across their owned seed sets.")
    parser.add_argument("--game-exe", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--seed", type=int, action="append")
    parser.add_argument("--mode", choices=("player", "qa"), default="qa")
    parser.add_argument("--policy", choices=("heuristic", "llm"), default="heuristic")
    parser.add_argument("--model", default=os.environ.get("QA_MODEL", ""))
    parser.add_argument("--api-url", default=os.environ.get("QA_API_URL"))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--deterministic-gate", action="store_true")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    scenario_path = args.project_root.resolve() / "config" / "qa-scenarios.json"
    try:
        scenarios = (
            [load_scenario(identifier, scenario_path) for identifier in args.scenario]
            if args.scenario
            else load_scenarios(scenario_path)
        )
        if args.deterministic_gate:
            validate_deterministic_gate_request(
                scenarios,
                args.seed,
                mode=args.mode,
                policy=args.policy,
            )
        results = run_benchmark(
            scenarios,
            args.seed,
            game_exe=args.game_exe,
            project_root=args.project_root,
            output_root=args.output,
            mode=args.mode,
            policy=args.policy,
            model=args.model,
            api_url=args.api_url,
            headless=args.headless,
            quiet=args.quiet,
        )
        summary = (
            verify_deterministic_gate(scenarios, results)
            if args.deterministic_gate
            else {
                "result": "pass" if all(result.return_code == 0 for result in results) else "fail",
                "runs": [
                    {
                        "scenario_id": result.scenario_id,
                        "seed": result.seed,
                        "output": str(result.output_dir),
                        "return_code": result.return_code,
                    }
                    for result in results
                ],
            }
        )
    except ScenarioContractError as error:
        print(json.dumps({"result": "contract_error", "error": str(error)}, ensure_ascii=False))
        sys.exit(2)
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(0 if summary["result"] == "pass" else 1)


if __name__ == "__main__":
    main()
