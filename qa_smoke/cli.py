from __future__ import annotations

import argparse
import json
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from .benchmark import run_benchmark
from .evaluation import evaluate_v4_oracle
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

    baseline_parser = subparsers.add_parser("baseline", help="Manage an explicit regression baseline.")
    baseline_subparsers = baseline_parser.add_subparsers(dest="baseline_action", required=True)
    set_parser = baseline_subparsers.add_parser("set", help="Approve a run directory as baseline.")
    set_parser.add_argument("run_dir", type=Path)
    set_parser.add_argument("--path", type=Path, default=Path("QAArtifacts/regression/baseline.json"))
    return parser.parse_args(argv)


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


def _v4_results_root(root: Path) -> Path:
    return root / "v4-core"


def _run_v4_suite(args: argparse.Namespace, root: Path, *, injected: bool) -> Path:
    output_root = _v4_results_root(root)
    output_root.mkdir(parents=True, exist_ok=True)
    ground_truth = load_v4_ground_truth()
    v4_scenarios = load_v4_scenarios()
    result_manifest: dict[str, Any] = {
        "schema_version": "qa-suite-run/v1",
        "suite": "v4-core",
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
            "--seed", str(selected_seed),
        ]
        fault_id = str((ground_truth.get(scenario.id) or {}).get("fault_id") or "") if injected else ""
        if fault_id:
            run_arguments.extend(["--fault", fault_id])
        if args.headless:
            run_arguments.append("--headless")
        if args.quiet:
            run_arguments.append("--quiet")
        return_code = run_session(parse_run_args(run_arguments))
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


def _run_legacy_suite(args: argparse.Namespace, root: Path) -> Path:
    output_root = root / "legacy-contract"
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
        inject_faults=False,
    )
    manifest = {
        "schema_version": "qa-suite-run/v1",
        "suite": "legacy-contract",
        "scenarios": {
            result.scenario_id: {
                "seed": result.seed,
                "return_code": result.return_code,
                "output_dir": str(result.output_dir),
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
        transitions = [
            json.loads(line)
            for line in steps_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        oracle = evaluate_v4_oracle(scenario.oracle.id, transitions)
    except (OSError, json.JSONDecodeError, ValueError):
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


def _verdict_from_artifact(path: Path) -> str:
    try:
        payload = json.loads((path / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
        results[scenario_id] = ScenarioResult(verdict, "stable")
    return results


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


def _write_suite_report(path: Path, results: dict[str, ScenarioResult]) -> None:
    lines = ["# QA Suite Run", "", "| Scenario | Verdict | Stability |", "|---|---|---|"]
    for scenario_id, result in sorted(results.items()):
        lines.append(f"| `{scenario_id}` | **{result.verdict}** | {result.stability} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_command(args: argparse.Namespace) -> int:
    root = _output_root(args)
    suite_roots: list[Path] = []
    if args.suite in {"v4-core", "all"}:
        suite_roots.append(
            _run_v4_suite(args, root, injected=args.command == "validate-faults")
        )
    if args.suite in {"legacy-contract", "all"}:
        suite_roots.append(_run_legacy_suite(args, root))
    current: dict[str, ScenarioResult] = {}
    for suite_root in suite_roots:
        current.update(_collect_suite_results(suite_root))
    suite_root = root if len(suite_roots) > 1 else suite_roots[0]
    if args.baseline is None:
        _write_suite_report(suite_root / "report.md", current)
        print(json.dumps({"suite_root": str(suite_root), "results": {key: value.verdict for key, value in current.items()}}, ensure_ascii=False))
        return 0 if all(result.verdict == "PASS" for result in current.values()) else 1
    baseline = load_baseline(args.baseline)
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


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_cli(argv)
    if args.command == "baseline":
        code = _baseline_set(args)
    else:
        code = _run_command(args)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
