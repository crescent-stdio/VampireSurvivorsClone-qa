"""Episode artifact helpers shared by external QA agents."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


class EpisodeArtifactError(RuntimeError):
    """Raised when a Unity episode artifact is missing or malformed."""


@dataclass(frozen=True)
class EpisodeSummary:
    path: Path
    payload: dict[str, object]


def load_unique_episode_summary(artifact_root: Path, seed: int) -> EpisodeSummary:
    """Load the only summary generated for ``seed`` under ``artifact_root``."""
    summaries = [
        episode / "summary.json"
        for episode in artifact_root.glob(f"episode-{seed:08d}-*")
        if episode.is_dir() and (episode / "summary.json").is_file()
    ]
    if len(summaries) != 1:
        raise EpisodeArtifactError(
            f"Seed {seed} produced {len(summaries)} episode summaries; exactly one is required."
        )

    summary_path = summaries[0]
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EpisodeArtifactError(f"Unable to read episode summary: {error}") from error
    if not isinstance(payload, dict):
        raise EpisodeArtifactError("Unable to read episode summary: root value must be an object.")
    return EpisodeSummary(path=summary_path, payload=payload)
