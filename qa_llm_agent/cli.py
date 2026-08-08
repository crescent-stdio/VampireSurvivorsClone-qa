from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from openai import OpenAI

from qa_llm_agent.artifacts import write_failure_artifact
from qa_llm_agent.async_driver import AsyncPolicyDriver
from qa_llm_agent.policy import OpenAiPolicy
from qa_llm_agent.runner import RunnerConfig, run_episode
from qa_llm_agent.scheduler import DecisionScheduler


DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_PLAYER = Path("QAArtifacts/player/QaGameplay.app")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Unity QA episode with an OpenAI movement policy.")
    parser.add_argument("--seed", type=int, required=True, help="Positive unused Unity QA seed.")
    parser.add_argument("--player", type=Path, default=DEFAULT_PLAYER, help="Path to QaGameplay.app.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI model ID.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.environ.get("OPENAI_API_KEY"):
        print("qa config: OPENAI_API_KEY is required.", file=sys.stderr)
        return 2
    scheduler = DecisionScheduler(period_seconds=2.0, attempt_budget=90)
    policy = OpenAiPolicy(
        client=OpenAI(max_retries=0),
        model=args.model,
        scheduler=scheduler,
        timeout_seconds=30.0,
    )
    try:
        result = run_episode(
            RunnerConfig(seed=args.seed, player=args.player, model=args.model),
            driver=AsyncPolicyDriver(policy),
            scheduler=scheduler,
        )
    except Exception as error:
        write_failure_artifact(
            Path("QAArtifacts"),
            seed=args.seed,
            model=args.model,
            error=error,
            secrets=(os.environ.get("OPENAI_API_KEY", ""),),
        )
        print(f"qa llm: {error}", file=sys.stderr)
        return 2
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
