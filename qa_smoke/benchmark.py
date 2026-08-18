from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .run import parse_args as parse_run_args
from .run import run_session
from .scenarios import Scenario, ScenarioContractError, load_scenario, load_scenarios


@dataclass(frozen=True)
class RunResult:
    scenario_id: str
    seed: int
    output_dir: Path
    return_code: int


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
    except ScenarioContractError as error:
        print(json.dumps({"result": "contract_error", "error": str(error)}, ensure_ascii=False))
        sys.exit(2)
    summary = {
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
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(0 if summary["result"] == "pass" else 1)


if __name__ == "__main__":
    main()
