"""QA preset definitions shared by Unity asset generation, shell wrappers, and Python agents.

``config/qa-presets.json`` is the single source of truth. The Unity editor generates
per-preset assets from it, shell wrappers query it through this module's CLI, and Python
agents import it directly.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys

SCHEMA = "qa-presets/v1"
DEFAULT_PRESET = "smoke"
FINGERPRINT_SCHEMA = "qa-preset-environment/v1"
FINGERPRINT_LENGTH = 16

DEFAULT_PRESET_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "qa-presets.json"
)


class PresetError(RuntimeError):
    """Raised when the preset definition file is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class LevelSettings:
    duration_seconds: float
    miniboss_spawn_seconds: float


@dataclass(frozen=True)
class CharacterSettings:
    health_multiplier: float
    armor: int


@dataclass(frozen=True)
class EpisodeSettings:
    deadline_seconds: float
    time_scale: float
    maximum_time_scale: float


@dataclass(frozen=True)
class ObservationSettings:
    elapsed_seconds_scale: float


@dataclass(frozen=True)
class RunSettings:
    seeds: tuple[int, ...]
    wall_clock_timeout_seconds: int


@dataclass(frozen=True)
class QaPreset:
    name: str
    description: str
    level: LevelSettings
    character: CharacterSettings
    episode: EpisodeSettings
    observation: ObservationSettings
    run: RunSettings

    @property
    def phase_thresholds(self) -> tuple[float, float, float]:
        """Return the (final boss, miniboss, mid) level-time thresholds.

        Derived rather than configured so the three values cannot drift apart from the
        level timings they describe.
        """
        return (
            self.level.duration_seconds,
            self.level.miniboss_spawn_seconds,
            self.level.miniboss_spawn_seconds / 2.0,
        )

    @property
    def canonical_environment(self) -> str:
        """Render the environment-defining fields in a language-neutral canonical form.

        Unity computes the same string in C#, so the format avoids anything whose
        rendering differs between runtimes: fields appear in a fixed order rather than a
        sorted one, and numbers use :func:`format_value` so integral values never carry a
        trailing ``.0``. Keep this in sync with ``QaPresetFingerprint`` on the C# side.
        Both test suites pin the resulting digests as literals, so a change on either
        side fails a test instead of silently splitting the two implementations.
        """
        fields = (
            ("level.durationSeconds", self.level.duration_seconds),
            ("level.minibossSpawnSeconds", self.level.miniboss_spawn_seconds),
            ("character.healthMultiplier", self.character.health_multiplier),
            ("character.armor", self.character.armor),
            ("episode.deadlineSeconds", self.episode.deadline_seconds),
            ("observation.elapsedSecondsScale", self.observation.elapsed_seconds_scale),
        )
        lines = [FINGERPRINT_SCHEMA]
        lines.extend(f"{key}={format_value(value)}" for key, value in fields)
        return "\n".join(lines)

    @property
    def fingerprint(self) -> str:
        """Identify the simulated environment this preset defines.

        Covers only fields that change the simulation or the meaning of an observation.
        ``episode.timeScale`` and ``run`` are excluded: Unity uses a fixed timestep, so
        the time scale changes wall-clock duration without changing the trajectory.
        This lets a policy trained under ``train`` be evaluated under ``eval`` while a
        ``smoke`` checkpoint is still rejected.
        """
        digest = hashlib.sha256(self.canonical_environment.encode("utf-8")).hexdigest()
        return digest[:FINGERPRINT_LENGTH]


def load_presets(path: Path | None = None) -> dict[str, QaPreset]:
    """Load every preset defined in ``path`` (defaults to ``config/qa-presets.json``)."""
    source = path or DEFAULT_PRESET_PATH
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except OSError as error:
        raise PresetError(f"Unable to read preset definitions: {error}") from error
    except json.JSONDecodeError as error:
        raise PresetError(f"Preset definitions are not valid JSON: {error}") from error

    if not isinstance(document, dict):
        raise PresetError("Preset definitions must be a JSON object.")
    schema = document.get("schema")
    if schema != SCHEMA:
        raise PresetError(
            f"Unsupported preset schema: {schema!r}; expected {SCHEMA!r}."
        )

    definitions = document.get("presets")
    if not isinstance(definitions, list) or not definitions:
        raise PresetError(
            "Preset definitions must contain a non-empty 'presets' array."
        )

    presets: dict[str, QaPreset] = {}
    for body in definitions:
        if not isinstance(body, dict):
            raise PresetError("Every entry in 'presets' must be an object.")
        preset = _build_preset(body.get("name"), body)
        if preset.name in presets:
            raise PresetError(f"Preset {preset.name!r} is defined more than once.")
        presets[preset.name] = preset
    if DEFAULT_PRESET not in presets:
        raise PresetError(
            f"Preset definitions must include the default preset {DEFAULT_PRESET!r}."
        )
    return presets


def load_preset(name: str = DEFAULT_PRESET, path: Path | None = None) -> QaPreset:
    """Load a single preset by name."""
    presets = load_presets(path)
    if name not in presets:
        available = ", ".join(sorted(presets))
        raise PresetError(
            f"Unknown preset {name!r}; available presets are {available}."
        )
    return presets[name]


def _build_preset(name: object, body: dict[str, object]) -> QaPreset:
    if not isinstance(name, str) or not name:
        raise PresetError("Every preset must declare a non-empty 'name'.")

    level = _section(name, body, "level")
    character = _section(name, body, "character")
    episode = _section(name, body, "episode")
    observation = _section(name, body, "observation")
    run = _section(name, body, "run")

    duration = _positive_float(name, level, "level", "durationSeconds")
    miniboss = _positive_float(name, level, "level", "minibossSpawnSeconds")
    if miniboss >= duration:
        raise PresetError(
            f"Preset {name!r} must spawn its miniboss before the level ends: "
            f"minibossSpawnSeconds {miniboss} >= durationSeconds {duration}."
        )

    deadline = _positive_float(name, episode, "episode", "deadlineSeconds")
    if deadline <= duration:
        raise PresetError(
            f"Preset {name!r} deadlineSeconds {deadline} must exceed durationSeconds {duration}; "
            "otherwise the final boss can never be defeated."
        )

    time_scale = _positive_float(name, episode, "episode", "timeScale")
    maximum_time_scale = _positive_float(name, episode, "episode", "maximumTimeScale")
    if time_scale > maximum_time_scale:
        raise PresetError(
            f"Preset {name!r} timeScale {time_scale} exceeds maximumTimeScale {maximum_time_scale}."
        )

    return QaPreset(
        name=name,
        description=_optional_text(name, body, "description"),
        level=LevelSettings(duration_seconds=duration, miniboss_spawn_seconds=miniboss),
        character=CharacterSettings(
            health_multiplier=_positive_float(
                name, character, "character", "healthMultiplier"
            ),
            armor=_non_negative_int(name, character, "character", "armor"),
        ),
        episode=EpisodeSettings(
            deadline_seconds=deadline,
            time_scale=time_scale,
            maximum_time_scale=maximum_time_scale,
        ),
        observation=ObservationSettings(
            elapsed_seconds_scale=_positive_float(
                name, observation, "observation", "elapsedSecondsScale"
            )
        ),
        run=RunSettings(
            seeds=_seeds(name, run),
            wall_clock_timeout_seconds=_positive_int(
                name, run, "run", "wallClockTimeoutSeconds"
            ),
        ),
    )


def _section(preset: str, body: dict[str, object], key: str) -> dict[str, object]:
    section = body.get(key)
    if not isinstance(section, dict):
        raise PresetError(f"Preset {preset!r} is missing the {key!r} section.")
    return section


def _optional_text(preset: str, body: dict[str, object], key: str) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise PresetError(f"Preset {preset!r} field {key!r} must be a string.")
    return value


def _number(preset: str, section: dict[str, object], group: str, key: str) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PresetError(f"Preset {preset!r} field {group}.{key} must be a number.")
    return float(value)


def _positive_float(
    preset: str, section: dict[str, object], group: str, key: str
) -> float:
    value = _number(preset, section, group, key)
    if value <= 0:
        raise PresetError(
            f"Preset {preset!r} field {group}.{key} must be positive; got {value}."
        )
    return value


def _integer(preset: str, section: dict[str, object], group: str, key: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PresetError(f"Preset {preset!r} field {group}.{key} must be an integer.")
    return value


def _positive_int(preset: str, section: dict[str, object], group: str, key: str) -> int:
    value = _integer(preset, section, group, key)
    if value <= 0:
        raise PresetError(
            f"Preset {preset!r} field {group}.{key} must be positive; got {value}."
        )
    return value


def _non_negative_int(
    preset: str, section: dict[str, object], group: str, key: str
) -> int:
    value = _integer(preset, section, group, key)
    if value < 0:
        raise PresetError(
            f"Preset {preset!r} field {group}.{key} must not be negative; got {value}."
        )
    return value


def _seeds(preset: str, section: dict[str, object]) -> tuple[int, ...]:
    value = section.get("seeds")
    if not isinstance(value, list) or not value:
        raise PresetError(
            f"Preset {preset!r} field run.seeds must be a non-empty array."
        )
    seeds: list[int] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int) or entry <= 0:
            raise PresetError(
                f"Preset {preset!r} field run.seeds must contain positive integers."
            )
        seeds.append(entry)
    if len(set(seeds)) != len(seeds):
        raise PresetError(f"Preset {preset!r} field run.seeds must not repeat a seed.")
    return tuple(seeds)


_KEYS = {
    "level.durationSeconds": lambda preset: preset.level.duration_seconds,
    "level.minibossSpawnSeconds": lambda preset: preset.level.miniboss_spawn_seconds,
    "character.healthMultiplier": lambda preset: preset.character.health_multiplier,
    "character.armor": lambda preset: preset.character.armor,
    "episode.deadlineSeconds": lambda preset: preset.episode.deadline_seconds,
    "episode.timeScale": lambda preset: preset.episode.time_scale,
    "episode.maximumTimeScale": lambda preset: preset.episode.maximum_time_scale,
    "observation.elapsedSecondsScale": lambda preset: (
        preset.observation.elapsed_seconds_scale
    ),
    "run.seeds": lambda preset: preset.run.seeds,
    "run.wallClockTimeoutSeconds": lambda preset: preset.run.wall_clock_timeout_seconds,
    "fingerprint": lambda preset: preset.fingerprint,
    "name": lambda preset: preset.name,
}


def format_value(value: object) -> str:
    """Render a preset value for shell consumption.

    Integral floats lose their fractional part so callers can pass the result straight
    into arguments such as ``-qaTimeScale=4`` and into shell numeric comparisons.
    """
    if isinstance(value, tuple):
        return " ".join(format_value(entry) for entry in value)
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query QA preset definitions.")
    parser.add_argument("--preset", default=DEFAULT_PRESET)
    parser.add_argument("--key", choices=sorted(_KEYS), help="Preset value to print.")
    parser.add_argument("--list", action="store_true", help="Print every preset name.")
    parser.add_argument("--path", type=Path, default=None)
    arguments = parser.parse_args(argv)

    try:
        if arguments.list:
            for name in sorted(load_presets(arguments.path)):
                print(name)
            return 0
        if arguments.key is None:
            parser.error("either --key or --list is required")
        preset = load_preset(arguments.preset, arguments.path)
    except PresetError as error:
        print(f"qa presets: {error}", file=sys.stderr)
        return 2
    print(format_value(_KEYS[arguments.key](preset)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
