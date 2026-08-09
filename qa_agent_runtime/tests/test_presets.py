import copy
import json
from pathlib import Path

import pytest

from qa_agent_runtime import presets


def _document() -> dict:
    return json.loads(presets.DEFAULT_PRESET_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "qa-presets.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_committed_definitions_provide_the_three_supported_presets() -> None:
    loaded = presets.load_presets()

    assert sorted(loaded) == ["eval", "smoke", "train"]


def test_smoke_preset_preserves_the_existing_regression_baseline() -> None:
    smoke = presets.load_preset("smoke")

    assert smoke.level.duration_seconds == 90.0
    assert smoke.level.miniboss_spawn_seconds == 45.0
    assert smoke.character.health_multiplier == 10.0
    assert smoke.character.armor == 100
    assert smoke.episode.deadline_seconds == 150.0
    assert smoke.episode.time_scale == 4.0
    assert smoke.episode.maximum_time_scale == 4.0
    assert smoke.observation.elapsed_seconds_scale == 600.0
    assert smoke.run.seeds == (8201, 8202, 8203, 8204, 8205, 8206, 8207, 8208, 8209, 8210)
    assert smoke.run.wall_clock_timeout_seconds == 60


def test_phase_thresholds_reproduce_the_previously_hardcoded_values() -> None:
    assert presets.load_preset("smoke").phase_thresholds == (90.0, 45.0, 22.5)


def test_training_and_evaluation_use_source_character_durability() -> None:
    for name in ("train", "eval"):
        character = presets.load_preset(name).character
        assert character.health_multiplier == 1.0
        assert character.armor == 0


def test_training_and_evaluation_differ_only_in_time_scale() -> None:
    train = presets.load_preset("train")
    evaluation = presets.load_preset("eval")

    assert train.episode.time_scale == 20.0
    assert evaluation.episode.time_scale == 1.0
    assert train.level == evaluation.level
    assert train.character == evaluation.character
    assert train.observation == evaluation.observation
    assert train.episode.deadline_seconds == evaluation.episode.deadline_seconds


def test_trained_policies_transfer_between_training_and_evaluation() -> None:
    assert presets.load_preset("train").fingerprint == presets.load_preset("eval").fingerprint


def test_smoke_assets_are_rejected_for_evaluation() -> None:
    assert presets.load_preset("smoke").fingerprint != presets.load_preset("eval").fingerprint


def test_fingerprint_ignores_time_scale_and_run_settings(tmp_path: Path) -> None:
    document = _document()
    baseline = presets.load_preset("eval", _write(tmp_path, copy.deepcopy(document)))
    document["presets"]["eval"]["episode"]["timeScale"] = 3.0
    document["presets"]["eval"]["episode"]["maximumTimeScale"] = 3.0
    document["presets"]["eval"]["run"]["seeds"] = [1]

    changed = presets.load_preset("eval", _write(tmp_path, document))

    assert changed.fingerprint == baseline.fingerprint


def test_fingerprint_tracks_character_durability(tmp_path: Path) -> None:
    document = _document()
    baseline = presets.load_preset("eval", _write(tmp_path, copy.deepcopy(document)))
    document["presets"]["eval"]["character"]["armor"] = 7

    changed = presets.load_preset("eval", _write(tmp_path, document))

    assert changed.fingerprint != baseline.fingerprint


def test_load_preset_rejects_an_unknown_name() -> None:
    with pytest.raises(presets.PresetError, match="Unknown preset 'soak'"):
        presets.load_preset("soak")


def test_load_presets_rejects_an_unsupported_schema(tmp_path: Path) -> None:
    document = _document()
    document["schema"] = "qa-presets/v2"

    with pytest.raises(presets.PresetError, match="Unsupported preset schema"):
        presets.load_presets(_write(tmp_path, document))


def test_load_presets_rejects_a_missing_default_preset(tmp_path: Path) -> None:
    document = _document()
    del document["presets"]["smoke"]

    with pytest.raises(presets.PresetError, match="must include the default preset"):
        presets.load_presets(_write(tmp_path, document))


def test_load_presets_rejects_a_miniboss_that_spawns_after_the_level_ends(tmp_path: Path) -> None:
    document = _document()
    document["presets"]["smoke"]["level"]["minibossSpawnSeconds"] = 120.0

    with pytest.raises(presets.PresetError, match="must spawn its miniboss before"):
        presets.load_presets(_write(tmp_path, document))


def test_load_presets_rejects_a_deadline_that_precedes_the_final_boss(tmp_path: Path) -> None:
    document = _document()
    document["presets"]["train"]["episode"]["deadlineSeconds"] = 60.0

    with pytest.raises(presets.PresetError, match="must exceed durationSeconds"):
        presets.load_presets(_write(tmp_path, document))


def test_load_presets_rejects_a_time_scale_above_its_maximum(tmp_path: Path) -> None:
    document = _document()
    document["presets"]["smoke"]["episode"]["timeScale"] = 8.0

    with pytest.raises(presets.PresetError, match="exceeds maximumTimeScale"):
        presets.load_presets(_write(tmp_path, document))


def test_load_presets_rejects_repeated_seeds(tmp_path: Path) -> None:
    document = _document()
    document["presets"]["smoke"]["run"]["seeds"] = [8201, 8201]

    with pytest.raises(presets.PresetError, match="must not repeat a seed"):
        presets.load_presets(_write(tmp_path, document))


def test_load_presets_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(presets.PresetError, match="Unable to read preset definitions"):
        presets.load_presets(tmp_path / "absent.json")


def test_format_value_renders_shell_friendly_output() -> None:
    assert presets.format_value(4.0) == "4"
    assert presets.format_value(22.5) == "22.5"
    assert presets.format_value((8201, 8202)) == "8201 8202"


def test_cli_prints_a_requested_value(capsys: pytest.CaptureFixture[str]) -> None:
    assert presets.main(["--preset", "smoke", "--key", "episode.timeScale"]) == 0

    assert capsys.readouterr().out.strip() == "4"


def test_cli_prints_seeds_for_shell_iteration(capsys: pytest.CaptureFixture[str]) -> None:
    assert presets.main(["--preset", "smoke", "--key", "run.seeds"]) == 0

    assert capsys.readouterr().out.split() == [str(seed) for seed in range(8201, 8211)]


def test_cli_lists_preset_names(capsys: pytest.CaptureFixture[str]) -> None:
    assert presets.main(["--list"]) == 0

    assert capsys.readouterr().out.split() == ["eval", "smoke", "train"]


def test_cli_reports_an_unknown_preset(capsys: pytest.CaptureFixture[str]) -> None:
    assert presets.main(["--preset", "soak", "--key", "name"]) == 2

    assert "qa presets: Unknown preset 'soak'" in capsys.readouterr().err
