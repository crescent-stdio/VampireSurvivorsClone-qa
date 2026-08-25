from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .benchmark import DETERMINISTIC_GATE_SCENARIO_IDS, run_benchmark
from .evaluation import evaluate_v4_oracle
from .memory import sanitize_error_type
from .regression import (
    DiffKind,
    ScenarioResult,
    diff_scenario_results,
    load_baseline,
    write_baseline,
)
from .run import parse_args as parse_run_args
from .run import run_session
from .scenarios import load_scenario, load_scenarios, load_v4_ground_truth, load_v4_scenarios


def parse_cli(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic gameplay QA suites.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a QA suite.")
    _add_run_options(run_parser)

    validate_parser = subparsers.add_parser("validate-faults", help="Run clean and injected fault pairs.")
    _add_run_options(validate_parser)
    validate_parser.set_defaults(command="validate-faults")

    detection_parser = subparsers.add_parser(
        "benchmark-detection",
        help="Run the blind clean/fault detection benchmark.",
    )
    detection_parser.add_argument("--build", type=Path, required=True)
    detection_parser.add_argument("--project-root", type=Path, default=Path.cwd())
    detection_parser.add_argument("--output", type=Path, default=None)
    detection_parser.add_argument("--profile", choices=("full", "poc"), default="full")
    detection_parser.add_argument("--api-url", default=None)
    detection_parser.add_argument("--headless", action="store_true")
    detection_parser.add_argument("--quiet", action="store_true")

    exploration_parser = subparsers.add_parser(
        "explore",
        help="Run clean-build autonomous exploration and candidate aggregation.",
    )
    exploration_parser.add_argument("--build", type=Path, required=True)
    exploration_parser.add_argument("--project-root", type=Path, default=Path.cwd())
    exploration_parser.add_argument("--output", type=Path, default=None)
    exploration_parser.add_argument("--profile", choices=("full", "poc"), default="full")
    exploration_parser.add_argument("--api-url", default=None)
    exploration_parser.add_argument("--headless", action="store_true")
    exploration_parser.add_argument("--quiet", action="store_true")
    exploration_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume only when all fixed campaign hashes match the existing checkpoint.",
    )

    baseline_parser = subparsers.add_parser("baseline", help="Manage an explicit regression baseline.")
    baseline_subparsers = baseline_parser.add_subparsers(dest="baseline_action", required=True)
    set_parser = baseline_subparsers.add_parser("set", help="Approve a run directory as baseline.")
    set_parser.add_argument("run_dir", type=Path)
    set_parser.add_argument("--path", type=Path, default=Path("QAArtifacts/regression/baseline.json"))
    args = parser.parse_args(argv)
    if args.command == "explore" and args.resume and args.output is None:
        parser.error("explore --resume requires an explicit --output")
    return args


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--suite", choices=("v4-core", "legacy-contract", "all"), default="v4-core")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--quiet", action="store_true")


def _output_root(args: argparse.Namespace) -> Path:
    if args.output is not None:
        return args.output.resolve()
    return (Path("QAArtifacts") / "runs" / uuid.uuid4().hex).resolve()


def _resolve_campaign_output(
    *,
    project_root: Path,
    output: Path | None,
    track: str,
    timestamp: datetime | None = None,
) -> Path:
    """Reserve a timestamped campaign directory when no output is provided."""

    if output is not None:
        return output
    if track not in {"track-a", "track-b"}:
        raise ValueError("campaign track must be track-a or track-b")
    current = timestamp or datetime.now().astimezone()
    base = (
        project_root.resolve()
        / "QAArtifacts"
        / "evaluation"
        / track
        / current.strftime("%Y%m%d-%H%M%S")
    )
    base.parent.mkdir(parents=True, exist_ok=True)
    for collision_index in range(1000):
        candidate = (
            base
            if collision_index == 0
            else base.with_name(f"{base.name}-{collision_index:02d}")
        )
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("unable to reserve timestamped campaign output")


def _suite_results_root(root: Path, suite: str, variant: str | None = None) -> Path:
    output_root = root / suite
    return output_root / variant if variant else output_root


def _run_v4_suite(
    args: argparse.Namespace,
    root: Path,
    *,
    injected: bool,
    variant: str | None = None,
) -> Path:
    output_root = _suite_results_root(root, "v4-core", variant)
    output_root.mkdir(parents=True, exist_ok=True)
    ground_truth = load_v4_ground_truth()
    v4_scenarios = load_v4_scenarios()
    result_manifest: dict[str, Any] = {
        "schema_version": "qa-suite-run/v1",
        "suite": "v4-core",
        "variant": variant,
        "injected": injected,
        "scenarios": {},
    }
    for scenario in v4_scenarios:
        legacy = load_scenario(scenario.legacy_scenario_id, args.project_root / "config" / "qa-scenarios.json")
        selected_seed = scenario.goal.verified_seed if args.seed is None else scenario.select_seed(args.seed)
        run_dir = output_root / scenario.id / str(selected_seed)
        run_arguments = [
            "--game-exe", str(args.build),
            "--project-root", str(args.project_root),
            "--output", str(run_dir),
            "--mode", "qa",
            "--policy", "heuristic",
            "--scenario", legacy.id,
            "--bridge-scenario-id", scenario.id,
            "--seed", str(selected_seed),
        ]
        fault_id = str((ground_truth.get(scenario.id) or {}).get("fault_id") or "") if injected else ""
        if fault_id:
            run_arguments.extend(["--fault", fault_id])
        if args.headless:
            run_arguments.append("--headless")
        if args.quiet:
            run_arguments.append("--quiet")
        run_args = parse_run_args(run_arguments)
        run_args.max_simulation_seconds = scenario.limits.max_simulation_seconds
        run_args.max_steps = scenario.limits.max_steps
        return_code = run_session(run_args)
        _rewrite_v4_verdict(run_dir, scenario)
        result_manifest["scenarios"][scenario.id] = {
            "legacy_scenario_id": legacy.id,
            "seed": selected_seed,
            "return_code": return_code,
            "output_dir": str(run_dir),
            "fault_id": fault_id or None,
        }
    manifest_path = output_root / "suite-manifest.json"
    manifest_path.write_text(json.dumps(result_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_root


def _run_legacy_suite(
    args: argparse.Namespace,
    root: Path,
    *,
    injected: bool = False,
    variant: str | None = None,
) -> Path:
    output_root = _suite_results_root(root, "legacy-contract", variant)
    scenarios = load_scenarios(args.project_root / "config" / "qa-scenarios.json")
    results = run_benchmark(
        scenarios,
        [args.seed] if args.seed is not None else None,
        game_exe=args.build,
        project_root=args.project_root,
        output_root=output_root,
        mode="qa",
        policy="heuristic",
        headless=args.headless,
        quiet=args.quiet,
        inject_faults=injected,
    )
    manifest = {
        "schema_version": "qa-suite-run/v1",
        "suite": "legacy-contract",
        "variant": variant,
        "injected": injected,
        "scenarios": {
            result.scenario_id: {
                "seed": result.seed,
                "return_code": result.return_code,
                "output_dir": str(result.output_dir),
                "fault_id": (
                    next(
                        (
                            scenario.ground_truth.fault_id
                            for scenario in scenarios
                            if scenario.id == result.scenario_id
                        ),
                        None,
                    )
                    if injected
                    else None
                ),
            }
            for result in results
        },
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "suite-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output_root


def _rewrite_v4_verdict(run_dir: Path, scenario: Any) -> None:
    """Apply the v4 oracle to the raw trace while preserving legacy artifacts."""

    verdict_path = run_dir / "verdict.json"
    steps_path = run_dir / "steps.jsonl"
    try:
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        if not isinstance(verdict, dict):
            raise ValueError("verdict.json must contain a JSON object")
        transitions = [
            json.loads(line)
            for line in steps_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if any(not isinstance(transition, dict) for transition in transitions):
            raise ValueError("steps.jsonl must contain JSON objects")
        oracle = evaluate_v4_oracle(scenario.oracle.id, transitions)
    except (OSError, json.JSONDecodeError) as error:
        _write_v4_error_verdict(
            verdict_path,
            scenario,
            "invalid v4 artifact",
            type(error).__name__,
        )
        return
    except (AttributeError, TypeError, ValueError) as error:
        _write_v4_error_verdict(
            verdict_path,
            scenario,
            "v4 oracle contract failure",
            type(error).__name__,
        )
        return
    verdict["schema_version"] = "qa-run-verdict/v2"
    verdict["v4_scenario_id"] = scenario.id
    verdict["v4_oracle_id"] = scenario.oracle.id
    verdict["oracle_verdict"] = oracle.verdict
    verdict["evidence_refs"] = list(dict.fromkeys(oracle.evidence_refs))
    if verdict.get("execution_status") != "completed":
        verdict["final_verdict"] = "ERROR"
    elif oracle.verdict == "fail":
        verdict["final_verdict"] = "FAIL"
    elif oracle.verdict == "not_evaluated":
        verdict["final_verdict"] = "NOT_REACHED"
    else:
        verdict["final_verdict"] = "PASS"
    verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_v4_error_verdict(
    verdict_path: Path,
    scenario: Any,
    category: str,
    error_type: str,
) -> None:
    try:
        payload = json.loads(verdict_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    safe_error_type = sanitize_error_type(error_type) or "Exception"
    payload.update(
        {
            "schema_version": "qa-run-verdict/v2",
            "v4_scenario_id": scenario.id,
            "v4_oracle_id": scenario.oracle.id,
            "oracle_verdict": "not_evaluated",
            "evidence_refs": [],
            "final_verdict": "ERROR",
            "error_type": safe_error_type,
            "error": f"{category}: details redacted",
        }
    )
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    verdict_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _verdict_from_artifact(path: Path) -> str:
    try:
        payload = json.loads((path / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "ERROR"
    if not isinstance(payload, dict):
        return "ERROR"
    if payload.get("final_verdict") in {"PASS", "FAIL", "NOT_REACHED", "ERROR"}:
        return str(payload["final_verdict"])
    if payload.get("execution_status") != "completed":
        return "ERROR"
    if payload.get("coverage_status") == "not_reached":
        return "NOT_REACHED"
    return "FAIL" if payload.get("oracle_verdict") == "fail" else "PASS"


def _collect_suite_results(suite_root: Path) -> dict[str, ScenarioResult]:
    manifest = json.loads((suite_root / "suite-manifest.json").read_text(encoding="utf-8"))
    results: dict[str, ScenarioResult] = {}
    for scenario_id, entry in (manifest.get("scenarios") or {}).items():
        verdict = _verdict_from_artifact(Path(entry["output_dir"]))
        results[_result_identity(manifest, scenario_id)] = ScenarioResult(verdict, "stable")
    return results


def _result_identity(manifest: dict[str, Any], scenario_id: str) -> str:
    suite = manifest.get("suite")
    variant = manifest.get("variant")
    if isinstance(suite, str) and suite and isinstance(variant, str) and variant:
        return f"{suite}/{variant}/{scenario_id}"
    return scenario_id


def _write_diff_report(path: Path, diffs: dict[str, Any]) -> None:
    counts = Counter(diff.kind.value for diff in diffs.values())
    lines = ["# QA Regression Diff", "", "## Summary", ""]
    for kind in DiffKind:
        if counts.get(kind.value):
            lines.append(f"- {kind.value}: {counts[kind.value]}")
    lines.extend(["", "## Scenarios", "", "| Scenario | Diff | Baseline | Current |", "|---|---|---|---|"])
    for scenario_id, diff in sorted(diffs.items()):
        before = diff.baseline.verdict if diff.baseline else "-"
        lines.append(f"| `{scenario_id}` | **{diff.kind.value}** | {before} | {diff.current.verdict} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _paired_clean_baseline(
    baseline: dict[str, ScenarioResult],
    current: dict[str, ScenarioResult],
) -> dict[str, ScenarioResult]:
    """Align an approved clean-run baseline only with paired clean identities."""

    aligned: dict[str, ScenarioResult] = {}
    for scenario_id, result in baseline.items():
        if scenario_id in current and "/clean/" in scenario_id:
            aligned[scenario_id] = result
            continue
        candidates = [
            current_id
            for current_id in current
            if "/clean/" in current_id and current_id.rsplit("/clean/", 1)[1] == scenario_id
        ]
        if len(candidates) == 1:
            aligned[candidates[0]] = result
        else:
            aligned[scenario_id] = result
    return aligned


def _authoritative_fault_bindings(
    args: argparse.Namespace,
) -> dict[str, dict[str, str | None]]:
    """Load the private suite registries used to verify paired run completeness."""

    bindings: dict[str, dict[str, str | None]] = {}
    if args.suite in {"v4-core", "all"}:
        scenarios = load_v4_scenarios()
        ground_truth = load_v4_ground_truth()
        scenario_ids = {scenario.id for scenario in scenarios}
        if scenario_ids != set(ground_truth):
            raise ValueError("v4 scenario and ground-truth registries must match")
        v4_bindings: dict[str, str | None] = {}
        for scenario in scenarios:
            fault_id = ground_truth[scenario.id].get("fault_id")
            if fault_id is not None and (
                not isinstance(fault_id, str) or not fault_id
            ):
                raise ValueError("v4 fault binding must be null or a non-empty string")
            if (scenario.bug_type == "control") != (fault_id is None):
                raise ValueError("v4 control and fault bindings must agree")
            v4_bindings[scenario.id] = fault_id
        bindings["v4-core"] = v4_bindings
    if args.suite in {"legacy-contract", "all"}:
        scenarios = load_scenarios(args.project_root / "config" / "qa-scenarios.json")
        scenario_by_id = {scenario.id: scenario for scenario in scenarios}
        if set(scenario_by_id) != set(DETERMINISTIC_GATE_SCENARIO_IDS):
            raise ValueError("legacy deterministic gate registry must match")
        bindings["legacy-contract"] = {
            scenario_id: scenario_by_id[scenario_id].ground_truth.fault_id
            for scenario_id in DETERMINISTIC_GATE_SCENARIO_IDS
        }
    return bindings


def _injected_oracle_expectations(
    suite_roots: Sequence[Path],
    authoritative_bindings: dict[str, dict[str, str | None]],
) -> dict[str, str]:
    """Return deterministic expected verdicts for injected fault and control runs."""

    expectations: dict[str, str] = {}
    paired_scenarios: dict[str, dict[str, set[str]]] = {}
    for suite_root in suite_roots:
        manifest = json.loads(
            (suite_root / "suite-manifest.json").read_text(encoding="utf-8")
        )
        if not isinstance(manifest, dict):
            raise ValueError("suite manifest must be an object")
        suite = manifest.get("suite")
        variant = manifest.get("variant")
        scenarios = manifest.get("scenarios")
        if not isinstance(suite, str) or not suite:
            raise ValueError("suite manifest requires a suite identity")
        if variant not in {"clean", "injected"}:
            raise ValueError("suite manifest requires a paired variant")
        if not isinstance(scenarios, dict):
            raise ValueError("suite manifest scenarios must be an object")
        scenario_ids = {str(scenario_id) for scenario_id in scenarios}
        if not scenario_ids:
            raise ValueError("paired suite scenarios must not be empty")
        expected_bindings = authoritative_bindings.get(suite)
        if expected_bindings is None or scenario_ids != set(expected_bindings):
            raise ValueError("suite scenarios must match the authoritative registry")
        variants = paired_scenarios.setdefault(suite, {})
        if variant in variants:
            raise ValueError("paired suite variant must be unique")
        variants[variant] = scenario_ids
        resolved_suite_root = suite_root.resolve()
        for scenario_id, entry in scenarios.items():
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("output_dir"), str)
                or not entry["output_dir"]
            ):
                raise ValueError("scenario output binding is invalid")
            try:
                Path(entry["output_dir"]).resolve().relative_to(resolved_suite_root)
            except (OSError, UnicodeError, ValueError) as error:
                raise ValueError("scenario output binding is invalid") from error
            if "fault_id" not in entry:
                raise ValueError("scenario requires an explicit fault binding")
            expected_fault_id = expected_bindings[str(scenario_id)]
            if variant == "clean" and entry["fault_id"] is not None:
                raise ValueError("clean scenario must not declare an injected fault")
            if variant == "injected" and entry["fault_id"] != expected_fault_id:
                raise ValueError("injected fault binding does not match the registry")
        if variant != "injected":
            continue
        for scenario_id, entry in scenarios.items():
            if not isinstance(entry, dict) or "fault_id" not in entry:
                raise ValueError("injected scenario requires an explicit fault binding")
            fault_id = expected_bindings[str(scenario_id)]
            if fault_id is not None and (not isinstance(fault_id, str) or not fault_id):
                raise ValueError("fault binding must be null or a non-empty string")
            identity = _result_identity(manifest, str(scenario_id))
            if identity in expectations:
                raise ValueError("duplicate injected scenario identity")
            expectations[identity] = "FAIL" if fault_id else "PASS"
    for variants in paired_scenarios.values():
        if set(variants) != {"clean", "injected"}:
            raise ValueError("suite requires one clean and one injected variant")
        if variants["clean"] != variants["injected"]:
            raise ValueError("paired variants require identical scenario identities")
    if set(paired_scenarios) != set(authoritative_bindings):
        raise ValueError("paired suites must match the requested registries")
    return expectations


def _injected_result_payload(
    injected: dict[str, ScenarioResult],
    expectations: dict[str, str],
) -> dict[str, dict[str, Any]]:
    return {
        scenario_id: {
            "verdict": result.verdict,
            "stability": result.stability,
            "expected_verdict": expectations[scenario_id],
            "matches_expected": result.verdict == expectations[scenario_id],
        }
        for scenario_id, result in injected.items()
    }


def _write_paired_diff_report(
    path: Path,
    clean_diffs: dict[str, Any],
    injected: dict[str, ScenarioResult],
    injected_expectations: dict[str, str],
) -> None:
    counts = Counter(diff.kind.value for diff in clean_diffs.values())
    lines = [
        "# QA Paired Regression Diff",
        "",
        "Baseline scope: paired clean variants only.",
        "Injected variants are excluded from the clean baseline diff and shown as current oracle results.",
        "",
        "## Clean baseline diff",
        "",
    ]
    for kind in DiffKind:
        if counts.get(kind.value):
            lines.append(f"- {kind.value}: {counts[kind.value]}")
    lines.extend(
        [
            "",
            "| Scenario | Diff | Baseline | Current |",
            "|---|---|---|---|",
        ]
    )
    for scenario_id, diff in sorted(clean_diffs.items()):
        before = diff.baseline.verdict if diff.baseline else "-"
        lines.append(
            f"| `{scenario_id}` | **{diff.kind.value}** | {before} | {diff.current.verdict} |"
        )
    lines.extend(
        [
            "",
            "## Injected current oracle results",
            "",
            "| Scenario | Verdict | Expected | Match | Stability |",
            "|---|---|---|---|---|",
        ]
    )
    for scenario_id, result in sorted(injected.items()):
        expected = injected_expectations[scenario_id]
        match = "yes" if result.verdict == expected else "no"
        lines.append(
            f"| `{scenario_id}` | **{result.verdict}** | **{expected}** | "
            f"{match} | {result.stability} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_suite_report(
    path: Path,
    results: dict[str, ScenarioResult],
    injected_expectations: dict[str, str] | None = None,
) -> None:
    if injected_expectations is None:
        lines = [
            "# QA Suite Run",
            "",
            "| Scenario | Verdict | Stability |",
            "|---|---|---|",
        ]
    else:
        lines = [
            "# QA Fault Validation",
            "",
            "Injected expectations are deterministic: fault-bearing variants must FAIL; controls must PASS.",
            "",
            "| Scenario | Verdict | Expected | Stability |",
            "|---|---|---|---|",
        ]
    for scenario_id, result in sorted(results.items()):
        if injected_expectations is None:
            lines.append(
                f"| `{scenario_id}` | **{result.verdict}** | {result.stability} |"
            )
        else:
            expected = (
                injected_expectations[scenario_id]
                if "/injected/" in scenario_id
                else "PASS"
            )
            lines.append(
                f"| `{scenario_id}` | **{result.verdict}** | **{expected}** | "
                f"{result.stability} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_fault_contract_error(root: Path) -> int:
    """Publish a bounded failure when paired evaluator metadata is invalid."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "report.md").write_text(
        "# QA Fault Validation\n\nEvaluation status: **ERROR**.\n\n"
        "The paired scenario contract is invalid.\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "suite_root": str(root),
                "status": "ERROR",
                "error_type": "CampaignContractError",
            },
            ensure_ascii=False,
        )
    )
    return 2


def _run_command(args: argparse.Namespace) -> int:
    root = _output_root(args)
    suite_roots: list[Path] = []
    if args.command == "validate-faults":
        variants = ((False, "clean"), (True, "injected"))
        if args.suite in {"v4-core", "all"}:
            for injected, variant in variants:
                suite_roots.append(
                    _run_v4_suite(args, root, injected=injected, variant=variant)
                )
        if args.suite in {"legacy-contract", "all"}:
            for injected, variant in variants:
                suite_roots.append(
                    _run_legacy_suite(args, root, injected=injected, variant=variant)
                )
    else:
        if args.suite in {"v4-core", "all"}:
            suite_roots.append(_run_v4_suite(args, root, injected=False))
        if args.suite in {"legacy-contract", "all"}:
            suite_roots.append(_run_legacy_suite(args, root))
    suite_root = root if len(suite_roots) > 1 else suite_roots[0]
    current: dict[str, ScenarioResult] = {}
    if args.command == "validate-faults":
        try:
            authoritative_bindings = _authoritative_fault_bindings(args)
            injected_expectations = _injected_oracle_expectations(
                suite_roots,
                authoritative_bindings,
            )
            for result_root in suite_roots:
                current.update(_collect_suite_results(result_root))
        except (KeyError, OSError, TypeError, UnicodeError, ValueError):
            return _write_fault_contract_error(suite_root)
    else:
        injected_expectations = {}
        for result_root in suite_roots:
            current.update(_collect_suite_results(result_root))
    if args.command == "validate-faults":
        injected_result_ids = {
            scenario_id for scenario_id in current if "/injected/" in scenario_id
        }
        if set(injected_expectations) != injected_result_ids:
            return _write_fault_contract_error(suite_root)
    if args.baseline is None:
        _write_suite_report(
            suite_root / "report.md",
            current,
            injected_expectations=(
                injected_expectations
                if args.command == "validate-faults"
                else None
            ),
        )
        print(json.dumps({"suite_root": str(suite_root), "results": {key: value.verdict for key, value in current.items()}}, ensure_ascii=False))
        if any(result.verdict == "ERROR" for result in current.values()):
            return 2
        if args.command == "validate-faults":
            clean_results = {
                scenario_id: result
                for scenario_id, result in current.items()
                if "/clean/" in scenario_id
            }
            injected_results = {
                scenario_id: result
                for scenario_id, result in current.items()
                if "/injected/" in scenario_id
            }
            if clean_results and injected_results:
                if any(result.verdict != "PASS" for result in clean_results.values()):
                    return 1
                if any(
                    result.verdict
                    != injected_expectations[scenario_id]
                    for scenario_id, result in injected_results.items()
                ):
                    return 1
                return 0
        return 0 if all(result.verdict == "PASS" for result in current.values()) else 1
    baseline = load_baseline(args.baseline)
    if args.command == "validate-faults":
        clean_current = {
            scenario_id: result
            for scenario_id, result in current.items()
            if "/clean/" in scenario_id
        }
        injected_current = {
            scenario_id: result
            for scenario_id, result in current.items()
            if "/injected/" in scenario_id
        }
        aligned_baseline = _paired_clean_baseline(baseline, clean_current)
        clean_diffs = diff_scenario_results(aligned_baseline, clean_current)
        diff_payload = {
            "schema_version": "qa-regression-diff/v2",
            "baseline_scope": "paired-clean-only",
            "clean_baseline_diffs": {
                scenario_id: diff.as_dict()
                for scenario_id, diff in clean_diffs.items()
            },
            "injected_current_results": _injected_result_payload(
                injected_current,
                injected_expectations,
            ),
        }
        (suite_root / "regression-diff.json").write_text(
            json.dumps(diff_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_paired_diff_report(
            suite_root / "report.md",
            clean_diffs,
            injected_current,
            injected_expectations,
        )
        print(
            json.dumps(
                {
                    "suite_root": str(suite_root),
                    "report": str(suite_root / "report.md"),
                    "baseline_scope": "paired-clean-only",
                },
                ensure_ascii=False,
            )
        )
        if any(result.verdict == "ERROR" for result in current.values()) or any(
            diff.kind == DiffKind.ERROR for diff in clean_diffs.values()
        ):
            return 2
        if any(
            diff.kind in {DiffKind.NEW_FAIL, DiffKind.STILL_FAIL}
            for diff in clean_diffs.values()
        ) or any(
            result.verdict != "PASS" for result in clean_current.values()
        ) or any(
            result.verdict != injected_expectations[scenario_id]
            for scenario_id, result in injected_current.items()
        ):
            return 1
        return 0
    diffs = diff_scenario_results(baseline, current)
    diff_payload = {scenario_id: diff.as_dict() for scenario_id, diff in diffs.items()}
    (suite_root / "regression-diff.json").write_text(
        json.dumps(diff_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_diff_report(suite_root / "report.md", diffs)
    print(json.dumps({"suite_root": str(suite_root), "report": str(suite_root / "report.md")}, ensure_ascii=False))
    if any(diff.kind == DiffKind.ERROR for diff in diffs.values()):
        return 2
    if any(diff.kind in {DiffKind.NEW_FAIL, DiffKind.STILL_FAIL} for diff in diffs.values()):
        return 1
    return 0


def _baseline_set(args: argparse.Namespace) -> int:
    suite_root = args.run_dir / "v4-core"
    results = _collect_suite_results(suite_root)
    write_baseline(args.path, results, suite="v4-core")
    print(args.path)
    return 0


def _benchmark_detection(args: argparse.Namespace) -> int:
    from .detection_campaign import (
        BenchmarkCampaignConfig,
        BridgeCampaignBackend,
        LLMInspectorAdapter,
        record_detection_initialization_failure,
        run_detection_campaign,
    )
    output = _resolve_campaign_output(
        project_root=args.project_root,
        output=args.output,
        track="track-b",
    )
    config = BenchmarkCampaignConfig(
        build=args.build,
        project_root=args.project_root,
        output=output,
        headless=args.headless,
        quiet=args.quiet,
        api_url=args.api_url,
        profile=args.profile,
    )
    try:
        backend = BridgeCampaignBackend(config)
        inspector = LLMInspectorAdapter(api_url=args.api_url)
    except Exception as error:
        safe_error_type = sanitize_error_type(type(error).__name__) or "Exception"
        try:
            record_detection_initialization_failure(config, error)
        except Exception as manifest_error:
            safe_error_type = (
                sanitize_error_type(type(manifest_error).__name__) or "Exception"
            )
        print(
            json.dumps(
                {"status": "incomplete", "error_type": safe_error_type},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        result = run_detection_campaign(
            config,
            backend=backend,
            inspector=inspector,
        )
    except Exception as error:
        safe_error_type = sanitize_error_type(type(error).__name__) or "Exception"
        print(
            json.dumps(
                {"status": "incomplete", "error_type": safe_error_type},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "pairs": len(result.pairs),
                "resumed": result.resumed,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _explore(args: argparse.Namespace) -> int:
    from .exploration_campaign import (
        BridgeExplorationBackend,
        ExplorationCampaignConfig,
        record_exploration_initialization_failure,
        run_exploration_campaign,
    )
    from .detection_campaign import LLMInspectorAdapter
    output = _resolve_campaign_output(
        project_root=args.project_root,
        output=args.output,
        track="track-a",
    )
    config = ExplorationCampaignConfig(
        build=args.build,
        project_root=args.project_root,
        output=output,
        headless=args.headless,
        quiet=args.quiet,
        api_url=args.api_url,
        resume=args.resume,
        profile=args.profile,
    )
    try:
        inspector = LLMInspectorAdapter(api_url=args.api_url)
    except Exception as error:
        safe_error_type = sanitize_error_type(type(error).__name__) or "Exception"
        try:
            record_exploration_initialization_failure(config, error)
        except Exception as manifest_error:
            safe_error_type = (
                sanitize_error_type(type(manifest_error).__name__) or "Exception"
            )
        print(
            json.dumps(
                {"status": "incomplete", "error_type": safe_error_type},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        result = run_exploration_campaign(
            config,
            backend=BridgeExplorationBackend(config),
            inspector=inspector,
        )
    except Exception as error:
        safe_error_type = sanitize_error_type(type(error).__name__) or "Exception"
        print(
            json.dumps(
                {"status": "incomplete", "error_type": safe_error_type},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "traces": len(result.records),
                "candidates": (report.get("summary") or {}).get("candidate_count", 0),
                "resumed": result.resumed,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_cli(argv)
    if args.command == "baseline":
        code = _baseline_set(args)
    elif args.command == "benchmark-detection":
        code = _benchmark_detection(args)
    elif args.command == "explore":
        code = _explore(args)
    else:
        code = _run_command(args)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
