"""Aggregate episode summaries across seeds into a single quality report.

A single evaluation episode is one sample. The scripted lane gets ten seeds through
``smoke.sh``; this is the equivalent for a learned policy, so a claim about a model is
backed by a distribution rather than one coin flip.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import statistics
import sys

from qa_agent_runtime.artifacts import EpisodeArtifactError, load_unique_episode_summary

OUTCOME_NAMES = {
    0: "InProgress",
    1: "Passed",
    2: "PlayerDied",
    3: "TimedOut",
    4: "Error",
}
FINAL_BOSS_EVENT = "phase:3"


@dataclass(frozen=True)
class EpisodeMetrics:
    seed: int
    outcome: str
    kill_count: int
    final_level: int
    elapsed_seconds: float
    damage_taken: float
    episode_return: float
    preset: str
    preset_fingerprint: str
    reached_final_boss: bool
    failure_reason: str


@dataclass(frozen=True)
class SweepReport:
    episodes: tuple[EpisodeMetrics, ...]

    @property
    def passed(self) -> int:
        return sum(1 for episode in self.episodes if episode.outcome == "Passed")

    @property
    def final_boss_rate(self) -> float:
        if not self.episodes:
            return 0.0
        return sum(1 for episode in self.episodes if episode.reached_final_boss) / len(
            self.episodes
        )

    def outcome_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for episode in self.episodes:
            counts[episode.outcome] = counts.get(episode.outcome, 0) + 1
        return counts

    def summarize(self, field: str) -> tuple[float, float]:
        """Return the mean and sample standard deviation of a numeric field."""
        values = [float(getattr(episode, field)) for episode in self.episodes]
        if not values:
            return (0.0, 0.0)
        deviation = statistics.stdev(values) if len(values) > 1 else 0.0
        return (statistics.fmean(values), deviation)

    def inconsistent_fingerprints(self) -> set[str]:
        return {episode.preset_fingerprint for episode in self.episodes}


def _number(payload: dict[str, object], key: str) -> float:
    value = payload.get(key)
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0.0
    )


def _text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def read_episode(artifact_root: Path, seed: int) -> EpisodeMetrics:
    summary = load_unique_episode_summary(artifact_root, seed)
    payload = summary.payload
    outcome = payload.get("Outcome")
    events = payload.get("ReplayDiscreteEvents")
    return EpisodeMetrics(
        seed=seed,
        outcome=OUTCOME_NAMES.get(outcome, str(outcome))
        if isinstance(outcome, int)
        else str(outcome),
        kill_count=int(_number(payload, "KillCount")),
        final_level=int(_number(payload, "FinalLevel")),
        elapsed_seconds=_number(payload, "ElapsedSeconds"),
        damage_taken=_number(payload, "DamageTaken"),
        episode_return=_number(payload, "EpisodeReturn"),
        preset=_text(payload, "Preset"),
        preset_fingerprint=_text(payload, "PresetFingerprint"),
        reached_final_boss=isinstance(events, list) and FINAL_BOSS_EVENT in events,
        failure_reason=_text(payload, "FailureReason"),
    )


def collect(artifact_root: Path, seeds: list[int]) -> SweepReport:
    return SweepReport(tuple(read_episode(artifact_root, seed) for seed in seeds))


def render(report: SweepReport) -> str:
    if not report.episodes:
        return "No episodes were collected."

    lines = [f"Evaluated {len(report.episodes)} episodes."]
    counts = report.outcome_counts()
    lines.append(
        "Outcomes: " + ", ".join(f"{name}={counts[name]}" for name in sorted(counts))
    )
    lines.append(
        f"Final-boss reach rate: {report.final_boss_rate:.2f} "
        f"({sum(1 for e in report.episodes if e.reached_final_boss)}/{len(report.episodes)})"
    )
    for label, field in (
        ("Kills", "kill_count"),
        ("Final level", "final_level"),
        ("Elapsed seconds", "elapsed_seconds"),
        ("Damage taken", "damage_taken"),
        ("Episode return", "episode_return"),
    ):
        mean, deviation = report.summarize(field)
        lines.append(f"{label}: mean {mean:.3f}, stdev {deviation:.3f}")

    fingerprints = report.inconsistent_fingerprints()
    if len(fingerprints) > 1:
        lines.append(
            "WARNING: episodes span more than one environment fingerprint "
            f"({', '.join(sorted(fingerprints))}); the aggregate is not comparable."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate QA episode summaries across seeds."
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--seeds", type=int, nargs="+", required=True)
    parser.add_argument("--json", type=Path, help="Also write the report as JSON.")
    arguments = parser.parse_args(argv)

    try:
        report = collect(arguments.artifact_root, arguments.seeds)
    except EpisodeArtifactError as error:
        print(f"qa sweep: {error}", file=sys.stderr)
        return 2

    print(render(report))
    if arguments.json is not None:
        arguments.json.parent.mkdir(parents=True, exist_ok=True)
        arguments.json.write_text(
            json.dumps(
                {
                    "episodes": [vars(episode) for episode in report.episodes],
                    "passed": report.passed,
                    "finalBossRate": report.final_boss_rate,
                    "outcomes": report.outcome_counts(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    if len(report.inconsistent_fingerprints()) > 1:
        return 2
    return 0 if report.passed == len(report.episodes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
