"""`bridge.state_file`: what survives a restart so `discovery.diff_removed` can work across one."""

from __future__ import annotations

from harmony_hub.bridge import state_file


def test_missing_file_reads_as_no_components_published_yet(tmp_path):
    assert state_file.load_components(tmp_path / "missing.json") == {}


def test_corrupt_file_falls_back_to_empty_instead_of_raising(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json", encoding="utf-8")
    assert state_file.load_components(path) == {}


def test_round_trips_the_component_map(tmp_path):
    path = tmp_path / "state.json"
    components = {"activity": "select", "scene_watch_tv": "scene"}
    state_file.save_components(components, path)
    assert state_file.load_components(path) == components


def test_saving_again_overwrites_rather_than_merges(tmp_path):
    path = tmp_path / "state.json"
    state_file.save_components({"a": "select"}, path)
    state_file.save_components({"b": "scene"}, path)
    assert state_file.load_components(path) == {"b": "scene"}
