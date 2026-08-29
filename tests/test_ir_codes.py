"""Tests for `harmony_hub.ir.codes` -- persistence for learned IR commands.

Mirrors the shape of `harmony_receiver.profiles`'s own tests: no hardware
involved, just round-tripping through a temp directory.
"""

from __future__ import annotations

from pathlib import Path

from harmony_hub.ir.codes import CodeSet, path_for


def test_a_missing_file_loads_as_an_empty_codeset(tmp_path: Path):
    codeset = CodeSet.load(tmp_path / "ir_living_room_tv.json")
    assert len(codeset) == 0
    assert list(codeset) == []


def test_add_then_save_then_load_round_trips(tmp_path: Path):
    path = tmp_path / "ir_soundbar.json"
    codeset = CodeSet()
    codeset.add(
        "volume_up",
        "Volume Up",
        [9000, 4500, 560, 1690],
        repeats=2,
        repeatable=True,
        decoded="NEC 0x04 0x08",
    )
    codeset.save(path)

    reloaded = CodeSet.load(path)
    assert len(reloaded) == 1
    command = reloaded.get("volume_up")
    assert command is not None
    assert command.label == "Volume Up"
    assert command.timings == [9000, 4500, 560, 1690]
    assert command.repeats == 2
    assert command.repeatable is True
    assert command.decoded == "NEC 0x04 0x08"
    assert command.learned_at  # stamped, not blank


def test_learning_the_same_name_again_replaces_it_rather_than_erroring(tmp_path: Path):
    codeset = CodeSet()
    codeset.add("power_toggle", "Power", [1, 2, 3])
    codeset.add("power_toggle", "Power", [4, 5, 6])
    assert len(codeset) == 1
    assert codeset.get("power_toggle").timings == [4, 5, 6]


def test_forget_drops_the_command(tmp_path: Path):
    codeset = CodeSet()
    codeset.add("mute", "Mute", [1, 2])
    codeset.forget("mute")
    assert len(codeset) == 0
    assert "mute" not in codeset


def test_forgetting_an_unknown_name_is_harmless():
    codeset = CodeSet()
    codeset.forget("nothing_here")
    assert len(codeset) == 0


def test_save_is_atomic_and_does_not_leave_a_temp_file_behind(tmp_path: Path):
    path = tmp_path / "ir_tv.json"
    codeset = CodeSet()
    codeset.add("power_on", "Power on", [1, 2])
    codeset.save(path)
    assert path.exists()
    assert list(tmp_path.iterdir()) == [path]


def test_iteration_is_sorted_by_name(tmp_path: Path):
    codeset = CodeSet()
    codeset.add("volume_up", "Volume Up", [1])
    codeset.add("channel_down", "Channel Down", [1])
    codeset.add("mute", "Mute", [1])
    assert [c.name for c in codeset] == ["channel_down", "mute", "volume_up"]


def test_path_for_uses_the_device_id_and_configured_directory():
    assert path_for("living_room_tv") == Path("codes") / "ir_living_room_tv.json"
    assert path_for("soundbar", codes_dir="data/codes") == Path("data/codes") / "ir_soundbar.json"
