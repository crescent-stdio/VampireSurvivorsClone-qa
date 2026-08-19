from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class DiffKind(str, Enum):
    NEW_FAIL = "NEW FAIL"
    FIXED = "FIXED"
    STILL_FAIL = "STILL FAIL"
    NEWLY_NOT_REACHED = "NEWLY NOT_REACHED"
    FLAKY = "FLAKY"
    ERROR = "ERROR"
    UNCHANGED = "UNCHANGED"
    NEW_SCENARIO = "NEW SCENARIO"


@dataclass(frozen=True)
class ScenarioResult:
    verdict: str
    stability: str = "stable"

    def __post_init__(self) -> None:
        if self.verdict not in {"PASS", "FAIL", "NOT_REACHED", "ERROR"}:
            raise ValueError(f"unsupported scenario verdict: {self.verdict}")
        if self.stability not in {"stable", "flaky"}:
            raise ValueError(f"unsupported scenario stability: {self.stability}")


@dataclass(frozen=True)
class ScenarioDiff:
    scenario_id: str
    kind: DiffKind
    baseline: ScenarioResult | None
    current: ScenarioResult

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "kind": self.kind.value,
            "baseline": asdict(self.baseline) if self.baseline else None,
            "current": asdict(self.current),
        }


def diff_scenario_results(
    baseline: dict[str, ScenarioResult],
    current: dict[str, ScenarioResult],
) -> dict[str, ScenarioDiff]:
    result: dict[str, ScenarioDiff] = {}
    for scenario_id in sorted(set(baseline) | set(current)):
        before = baseline.get(scenario_id)
        after = current.get(scenario_id)
        if after is None:
            continue
        if after.stability == "flaky" or (before and before.stability == "flaky"):
            kind = DiffKind.FLAKY
        elif after.verdict == "ERROR":
            kind = DiffKind.ERROR
        elif before is None:
            kind = DiffKind.NEW_FAIL if after.verdict == "FAIL" else DiffKind.NEW_SCENARIO
        elif before.verdict == "PASS" and after.verdict == "FAIL":
            kind = DiffKind.NEW_FAIL
        elif before.verdict == "FAIL" and after.verdict == "PASS":
            kind = DiffKind.FIXED
        elif before.verdict == "FAIL" and after.verdict == "FAIL":
            kind = DiffKind.STILL_FAIL
        elif before.verdict == "PASS" and after.verdict == "NOT_REACHED":
            kind = DiffKind.NEWLY_NOT_REACHED
        else:
            kind = DiffKind.UNCHANGED
        result[scenario_id] = ScenarioDiff(scenario_id, kind, before, after)
    return result


def load_baseline(path: Path) -> dict[str, ScenarioResult]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid baseline file {path}: {error}") from error
    scenarios = payload.get("scenarios") if isinstance(payload, dict) else None
    if not isinstance(scenarios, dict):
        raise ValueError("baseline.scenarios must be an object")
    return {
        scenario_id: ScenarioResult(
            str(value.get("verdict")), str(value.get("stability", "stable"))
        )
        for scenario_id, value in scenarios.items()
        if isinstance(value, dict)
    }


def write_baseline(
    path: Path,
    results: dict[str, ScenarioResult],
    *,
    build_hash: str = "",
    suite: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "qa-regression-baseline/v1",
        "build_hash": build_hash,
        "suite": suite,
        "scenarios": {scenario_id: asdict(result) for scenario_id, result in sorted(results.items())},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
