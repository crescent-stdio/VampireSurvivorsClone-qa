import json
from pathlib import Path

import pytest

from qa_agent_runtime import artifacts


def test_load_unique_episode_summary_returns_the_matching_path_and_payload(tmp_path: Path) -> None:
    episode = tmp_path / "episode-00000123-one"
    episode.mkdir()
    summary_path = episode / "summary.json"
    summary_path.write_text(json.dumps({"Seed": 123, "Outcome": 1}), encoding="utf-8")

    summary = artifacts.load_unique_episode_summary(tmp_path, 123)

    assert summary.path == summary_path
    assert summary.payload == {"Seed": 123, "Outcome": 1}


@pytest.mark.parametrize("count", [0, 2])
def test_load_unique_episode_summary_rejects_missing_or_ambiguous_matches(
    tmp_path: Path,
    count: int,
) -> None:
    for index in range(count):
        episode = tmp_path / f"episode-00000123-{index}"
        episode.mkdir()
        (episode / "summary.json").write_text("{}", encoding="utf-8")

    with pytest.raises(artifacts.EpisodeArtifactError, match=f"produced {count} episode summaries"):
        artifacts.load_unique_episode_summary(tmp_path, 123)


def test_load_unique_episode_summary_rejects_invalid_json(tmp_path: Path) -> None:
    episode = tmp_path / "episode-00000123-invalid"
    episode.mkdir()
    (episode / "summary.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(artifacts.EpisodeArtifactError, match="Unable to read episode summary"):
        artifacts.load_unique_episode_summary(tmp_path, 123)
