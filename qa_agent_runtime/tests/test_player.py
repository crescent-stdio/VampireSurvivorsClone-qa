from pathlib import Path

import pytest

from qa_agent_runtime import player


@pytest.mark.parametrize(
    ("platform", "suffix"),
    [
        ("darwin", ".app"),
        ("win32", ".exe"),
        ("win_amd64", ".exe"),
        ("linux", ".x86_64"),
    ],
)
def test_each_platform_gets_the_shape_unity_produces(
    platform: str, suffix: str
) -> None:
    assert player.player_suffix(platform) == suffix
    assert player.default_player(platform=platform).name == f"QaGameplay{suffix}"


def test_the_default_player_sits_where_a_distributed_build_is_unpacked() -> None:
    # Teammates unpack their platform's build into QAArtifacts/player, so the default
    # resolves without anyone passing --player.
    assert player.default_player(platform="linux") == Path(
        "QAArtifacts/player/QaGameplay.x86_64"
    )


def test_a_macos_bundle_is_accepted_as_a_directory(tmp_path: Path) -> None:
    bundle = tmp_path / "QaGameplay.app"
    bundle.mkdir()

    assert player.validate_player(bundle) == bundle


def test_resolve_executable_reads_bundle_executable(tmp_path: Path) -> None:
    bundle = tmp_path / "VampireSurvivorsClone.app"
    executable_directory = bundle / "Contents" / "MacOS"
    executable_directory.mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0"?><plist version="1.0"><dict>'
        "<key>CFBundleExecutable</key><string>VampireSurvivorsClone</string>"
        "</dict></plist>",
        encoding="utf-8",
    )
    executable = executable_directory / "VampireSurvivorsClone"
    executable.touch()

    assert player.resolve_executable(bundle) == executable


def test_resolve_executable_passes_through_plain_file(tmp_path: Path) -> None:
    executable = tmp_path / "VampireSurvivorsClone.exe"
    executable.touch()

    assert player.resolve_executable(executable) == executable


def test_resolve_executable_reports_missing_bundle_key(tmp_path: Path) -> None:
    bundle = tmp_path / "Broken.app"
    contents = bundle / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_text(
        '<?xml version="1.0"?><plist version="1.0"><dict></dict></plist>',
        encoding="utf-8",
    )

    with pytest.raises(player.PlayerError, match="CFBundleExecutable"):
        player.resolve_executable(bundle)


@pytest.mark.parametrize("name", ["QaGameplay.x86_64", "QaGameplay.exe"])
def test_other_platforms_are_accepted_as_files(tmp_path: Path, name: str) -> None:
    executable = tmp_path / name
    executable.write_bytes(b"")

    assert player.validate_player(executable) == executable


def test_a_bundle_that_is_a_file_is_rejected(tmp_path: Path) -> None:
    # Checking existence alone would accept a stray file named like a bundle.
    impostor = tmp_path / "QaGameplay.app"
    impostor.write_bytes(b"")

    with pytest.raises(player.PlayerError, match="bundle does not exist"):
        player.validate_player(impostor)


def test_an_executable_that_is_a_directory_is_rejected(tmp_path: Path) -> None:
    # An archive unpacked one level too deep leaves a directory with the right name.
    impostor = tmp_path / "QaGameplay.exe"
    impostor.mkdir()

    with pytest.raises(player.PlayerError, match="executable does not exist"):
        player.validate_player(impostor)


def test_an_unrecognized_suffix_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "QaGameplay.zip"
    archive.write_bytes(b"")

    with pytest.raises(player.PlayerError, match="Unrecognized Unity player suffix"):
        player.validate_player(archive)


def test_a_missing_player_names_the_path_it_looked_for(tmp_path: Path) -> None:
    with pytest.raises(player.PlayerError, match="QaGameplay.x86_64"):
        player.validate_player(tmp_path / "QaGameplay.x86_64")


def test_artifacts_land_beside_the_player_on_every_platform() -> None:
    # Unity resolves a relative artifact directory against the launched player's
    # directory, which is the same rule on all three platforms.
    for name in ("QaGameplay.app", "QaGameplay.x86_64", "QaGameplay.exe"):
        assert player.artifact_root(Path("build/player") / name) == Path(
            "build/player/QAArtifacts"
        )
