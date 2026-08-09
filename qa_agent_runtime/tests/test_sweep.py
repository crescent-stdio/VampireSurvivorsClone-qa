import json
from pathlib import Path

import pytest

from qa_agent_runtime import sweep


def write_episode(
    artifact_root: Path,
    seed: int,
    *,
    outcome: int = 1,
    kills: int = 10,
    level: int = 3,
    elapsed: float = 100.0,
    damage: float = 0.25,
    episode_return: float = 5.0,
    fingerprint: str = "4ed498f8f2386317",
    final_boss: bool = True,
) -> None:
    episode = artifact_root / f"episode-{seed:08d}-fake"
    episode.mkdir(parents=True)
    (episode / "summary.json").write_text(
        json.dumps(
            {
                "Seed": seed,
                "Outcome": outcome,
                "KillCount": kills,
                "FinalLevel": level,
                "ElapsedSeconds": elapsed,
                "DamageTaken": damage,
                "EpisodeReturn": episode_return,
                "Preset": "eval",
                "PresetFingerprint": fingerprint,
                "FailureReason": "" if outcome == 1 else "PlayerDied",
                "ReplayDiscreteEvents": ["phase:0", "phase:3"] if final_boss else ["phase:0"],
            }
        ),
        encoding="utf-8",
    )


def test_a_sweep_reports_the_distribution_rather_than_one_sample(tmp_path: Path) -> None:
    write_episode(tmp_path, 1, kills=10, outcome=1)
    write_episode(tmp_path, 2, kills=20, outcome=2, final_boss=False)

    report = sweep.collect(tmp_path, [1, 2])

    assert report.outcome_counts() == {"Passed": 1, "PlayerDied": 1}
    assert report.passed == 1
    assert report.final_boss_rate == pytest.approx(0.5)
    mean, deviation = report.summarize("kill_count")
    assert mean == pytest.approx(15.0)
    assert deviation == pytest.approx(7.0710678, rel=1e-5)


def test_a_single_episode_reports_zero_deviation(tmp_path: Path) -> None:
    write_episode(tmp_path, 1)

    mean, deviation = sweep.collect(tmp_path, [1]).summarize("kill_count")

    assert mean == pytest.approx(10.0)
    assert deviation == 0.0


def test_mixing_environments_is_reported_rather_than_averaged(tmp_path: Path) -> None:
    # Averaging across fingerprints would produce a number that describes no environment.
    write_episode(tmp_path, 1, fingerprint="4ed498f8f2386317")
    write_episode(tmp_path, 2, fingerprint="a75a0e8505ba6431")

    report = sweep.collect(tmp_path, [1, 2])

    assert len(report.inconsistent_fingerprints()) == 2
    assert "more than one environment fingerprint" in sweep.render(report)


def test_the_cli_writes_a_json_report_and_passes_only_when_every_seed_passed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    write_episode(artifacts, 1)
    write_episode(artifacts, 2)
    report_path = tmp_path / "reports" / "sweep.json"

    code = sweep.main(
        ["--artifact-root", str(artifacts), "--seeds", "1", "2", "--json", str(report_path)]
    )

    assert code == 0
    assert "Evaluated 2 episodes." in capsys.readouterr().out
    assert json.loads(report_path.read_text(encoding="utf-8"))["passed"] == 2


def test_the_cli_reports_a_gameplay_failure_without_treating_it_as_infrastructure(
    tmp_path: Path,
) -> None:
    write_episode(tmp_path, 1, outcome=2)

    assert sweep.main(["--artifact-root", str(tmp_path), "--seeds", "1"]) == 1


def test_the_cli_rejects_mixed_environments(tmp_path: Path) -> None:
    write_episode(tmp_path, 1, fingerprint="4ed498f8f2386317")
    write_episode(tmp_path, 2, fingerprint="a75a0e8505ba6431")

    assert sweep.main(["--artifact-root", str(tmp_path), "--seeds", "1", "2"]) == 2


def test_a_missing_episode_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sweep.main(["--artifact-root", str(tmp_path), "--seeds", "1"]) == 2

    assert "qa sweep:" in capsys.readouterr().err
