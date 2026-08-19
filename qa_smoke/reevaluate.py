"""Recompute verdicts for run directories that already hold a usable trace.

A run aborted by a rate limit, a truncated response or a player crash still
recorded every observation it reached. Those artifacts predate the partial-trace
evaluation in `build_session_verdict`, so their verdict.json says
"not_evaluated" even though the trace proves a fault. This rewrites them in
place using exactly the same helper the live runner uses, so the two can never
disagree about whether a partial pass is trustworthy.

Usage:
    python -m qa_smoke.reevaluate [--dry-run] <run_dir> [<run_dir> ...]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .detection import AUTHORITY_NOTE, score_agent_detection
from .evaluation import fault_evidence_refs, scenario_verdict_axes
from .hypotheses import HypothesisTracker
from .reporting import final_verdict
from .scenarios import ScenarioContractError, load_scenario, load_v4_scenarios


class ReevaluationSkipped(Exception):
    """The directory cannot be judged; it is reported and left untouched."""


def load_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        verdict = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
        transitions = [
            json.loads(line)
            for line in (run_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as error:
        raise ReevaluationSkipped(f"unreadable artifacts: {error}") from error
    return manifest, verdict, transitions_for_run(manifest, transitions)


def transitions_for_run(
    manifest: dict[str, Any], transitions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep only the rows this manifest's run wrote.

    steps.jsonl is opened in append mode, so reusing an output directory leaves
    several sessions concatenated in one file. Judging the union mixes traces
    from different runs -- including, in practice, a fault-injected session
    bleeding into a fault-free one and manufacturing a control false positive.
    The live runner is unaffected because it evaluates its in-memory steps.
    """
    run_id = str(manifest.get("run_id") or "")
    if not run_id:
        return transitions
    return [row for row in transitions if str(row.get("run_id") or "") == run_id]


def resolve_scenario(scenario_id: str):
    """Resolve a v1 id directly, or a v4 id through its legacy scenario.

    v4 runs record the v4 id in the manifest while driving the legacy scenario, so
    looking only at the v1 registry skipped every v4 artifact.
    """
    try:
        return load_scenario(scenario_id)
    except (ScenarioContractError, OSError) as error:
        try:
            v4 = {item.id: item for item in load_v4_scenarios()}[scenario_id]
            return load_scenario(v4.legacy_scenario_id)
        except (KeyError, ScenarioContractError, OSError, AttributeError):
            raise ReevaluationSkipped(f"unknown scenario {scenario_id!r}: {error}") from error


def replay_hypotheses(transitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild hypothesis states by replaying the tracker over the trace.

    The per-row hypothesis_state snapshots are point-in-time and miss later
    demotions, so replaying the same class over the same rows in the same order is
    what keeps the offline score identical to the live one.
    """
    tracker = HypothesisTracker()
    for transition in transitions:
        decision = transition.get("decision")
        if isinstance(decision, dict):
            try:
                tracker.observe_decision(decision)
            except (ValueError, TypeError):
                continue
    return tracker.snapshot()


def read_assessment(run_dir: Path) -> dict[str, Any] | None:
    try:
        critic = json.loads((run_dir / "critic.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    assessment = critic.get("llm_assessment")
    return assessment if isinstance(assessment, dict) else None


def reevaluate_run(run_dir: Path) -> dict[str, Any]:
    """Return the rewritten verdict for one run directory."""
    manifest, verdict, transitions = load_run(run_dir)
    scenario_id = str(manifest.get("scenario_id") or "")
    if not scenario_id:
        raise ReevaluationSkipped("run has no scenario_id; there is no oracle to apply")
    scenario = resolve_scenario(scenario_id)

    # execution_status records why the run ended and is never revised here.
    execution_status = str(verdict.get("execution_status") or "infrastructure_error")
    axes = scenario_verdict_axes(scenario, transitions, execution_status)
    if axes is None:
        raise ReevaluationSkipped(
            f"trace holds nothing judgeable ({len(transitions)} transitions for this run)"
        )

    updated = dict(verdict)
    updated["coverage_status"] = axes.coverage_status
    updated["oracle_verdict"] = axes.oracle_verdict
    updated["evidence_refs"] = list(axes.evidence_refs)
    updated["trace_completeness"] = (
        "complete" if execution_status == "completed" else "partial"
    )
    updated["final_verdict"] = final_verdict(
        execution_status,
        axes.coverage_status,
        axes.oracle_verdict,
        [],
    )
    detection = score_agent_detection(
        fault_id=manifest.get("fault_id"),
        policy=str(manifest.get("policy") or ""),
        trace_completeness=updated["trace_completeness"],
        oracle_verdict=axes.oracle_verdict,
        transitions=transitions,
        fault_refs=fault_evidence_refs(scenario, transitions),
        hypotheses=replay_hypotheses(transitions),
        llm_assessment=read_assessment(run_dir),
    )
    updated["agent_detection"] = detection.status
    updated["_detection_payload"] = {
        "schema_version": "qa-agent-detection/v1",
        "run_id": manifest.get("run_id"),
        "scenario_id": manifest.get("scenario_id"),
        "fault_id": manifest.get("fault_id"),
        "policy": manifest.get("policy"),
        "prompt_version": manifest.get("prompt_version"),
        **detection.model_dump(),
        "authority_note": AUTHORITY_NOTE,
    }
    return updated


def describe_change(before: dict[str, Any], after: dict[str, Any]) -> str:
    fields = (
        "coverage_status",
        "oracle_verdict",
        "agent_detection",
        "final_verdict",
        "trace_completeness",
    )
    changes = [
        f"{name}: {before.get(name, 'absent')} -> {after.get(name)}"
        for name in fields
        if before.get(name) != after.get(name)
    ]
    return ", ".join(changes) if changes else "no change"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dirs", type=Path, nargs="+")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing verdict.json.",
    )
    args = parser.parse_args(argv)

    rewritten = 0
    for run_dir in args.run_dirs:
        try:
            _, before, _ = load_run(run_dir)
            after = reevaluate_run(run_dir)
        except ReevaluationSkipped as error:
            print(f"[skip] {run_dir}: {error}", flush=True)
            continue
        # Separate the sidecar before comparing, so a re-run of an already-scored
        # directory compares verdict to verdict and reports no change.
        detection_payload = after.pop("_detection_payload", None)
        summary = describe_change(before, after)
        if args.dry_run:
            print(f"[dry-run] {run_dir}: {summary}", flush=True)
            continue
        if before == after:
            print(f"[ok] {run_dir}: {summary}", flush=True)
            continue
        (run_dir / "verdict.json").write_text(
            json.dumps(after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if detection_payload is not None:
            (run_dir / "agent-detection.json").write_text(
                json.dumps(detection_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        rewritten += 1
        print(f"[rewrote] {run_dir}: {summary}", flush=True)
    return 0 if rewritten or args.dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
