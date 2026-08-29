"""State transitions are pure functions here on purpose -- no filesystem needed to test the bookkeeping itself."""

from __future__ import annotations

import json

import pytest

from harmony_hub.update.state import (
    NoPreviousRelease,
    UpdateState,
    activate,
    confirm,
    list_releases,
    load,
    prune,
    release_dir,
    rollback,
    save,
)


def test_activating_a_release_makes_it_current_and_puts_it_on_trial():
    state = UpdateState(current="build-1")
    new_state = activate(state, "build-2")

    assert new_state.current == "build-2"
    assert new_state.previous == "build-1"
    assert new_state.trial.release == "build-2"
    assert new_state.trial.attempts == 0
    assert new_state.trial.from_release == "build-1"


def test_activating_the_first_ever_release_has_no_previous():
    state = activate(UpdateState(), "build-1")
    assert state.current == "build-1"
    assert state.previous is None
    assert state.trial.from_release is None


def test_confirming_clears_the_trial_and_records_history():
    state = activate(UpdateState(current="build-1"), "build-2")
    confirmed = confirm(state)

    assert confirmed.trial is None
    assert len(confirmed.history) == 1
    assert confirmed.history[0].build_id == "build-2"
    assert confirmed.history[0].outcome == "good"


def test_confirming_with_no_trial_active_is_a_no_op():
    state = UpdateState(current="build-1")
    assert confirm(state) == state


def test_rollback_swaps_current_and_previous_and_clears_the_trial():
    state = activate(UpdateState(current="build-1"), "build-2")
    rolled_back = rollback(state)

    assert rolled_back.current == "build-1"
    assert rolled_back.previous == "build-2"
    assert rolled_back.trial is None
    assert rolled_back.history[-1].outcome == "rolled_back"
    assert rolled_back.history[-1].build_id == "build-2"


def test_rolling_back_twice_returns_to_where_you_started():
    original = UpdateState(current="build-1", previous="build-0")
    twice = rollback(rollback(original))
    assert twice.current == original.current
    assert twice.previous == original.previous


def test_rollback_with_nothing_to_roll_back_to_raises():
    with pytest.raises(NoPreviousRelease):
        rollback(UpdateState(current="build-1"))


def test_history_is_capped_so_it_cannot_grow_without_bound():
    state = UpdateState()
    for i in range(60):
        state = confirm(activate(state, f"build-{i}"))
    assert len(state.history) == 50
    assert state.history[-1].build_id == "build-59"


def test_state_round_trips_through_disk_with_the_from_alias(tmp_path):
    path = tmp_path / "update_state.json"
    state = activate(UpdateState(current="build-1", last_nonce=42), "build-2")
    save(state, path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["trial"]["from"] == "build-1"  # the launcher reads this key directly, stdlib json only

    restored = load(path)
    assert restored == state


def test_loading_a_missing_file_returns_a_fresh_state(tmp_path):
    assert load(tmp_path / "nope.json") == UpdateState()


def test_loading_a_corrupt_file_returns_a_fresh_state_rather_than_raising(tmp_path):
    path = tmp_path / "update_state.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load(path) == UpdateState()


def test_list_releases_ignores_tmp_staging_directories(tmp_path):
    (tmp_path / "releases" / "build-1").mkdir(parents=True)
    (tmp_path / "releases" / "build-2").mkdir(parents=True)
    (tmp_path / "releases" / "build-3.tmp").mkdir(parents=True)
    assert list_releases(tmp_path) == ["build-1", "build-2"]


def test_list_releases_on_a_fresh_root_is_empty(tmp_path):
    assert list_releases(tmp_path) == []


def test_prune_always_keeps_current_and_previous_even_if_old(tmp_path):
    for build_id in ["build-1", "build-2", "build-3", "build-4", "build-5"]:
        release_dir(tmp_path, build_id).mkdir(parents=True)

    # keep=1 only guarantees the single newest by name beyond current/previous
    # -- here that is build-5, which is already `current`, so nothing besides
    # current/previous survives.
    state = UpdateState(current="build-5", previous="build-1")
    removed = prune(tmp_path, state, keep=1)

    assert set(removed) == {"build-2", "build-3", "build-4"}
    assert list_releases(tmp_path) == ["build-1", "build-5"]


def test_prune_keeps_the_newest_n_beyond_current_and_previous(tmp_path):
    for build_id in ["build-1", "build-2", "build-3", "build-4"]:
        release_dir(tmp_path, build_id).mkdir(parents=True)

    state = UpdateState(current="build-4", previous="build-3")
    removed = prune(tmp_path, state, keep=3)

    # The newest 3 by name (build-4, build-3, build-2) already cover
    # current/previous, so only build-1 falls outside that set.
    assert removed == ["build-1"]
    assert list_releases(tmp_path) == ["build-2", "build-3", "build-4"]
