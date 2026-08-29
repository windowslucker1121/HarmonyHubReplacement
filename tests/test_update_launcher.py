"""The launcher's own decision logic: which release to boot, and when to give up on a trial.

Stdlib only, deliberately -- see `launcher.py`'s own docstring for why. These
tests exercise `decide_release` directly against a fake root; `main()`
itself (the `execve` at the end) is not covered here since replacing the
test process's image is not something a unit test should risk doing.
"""

from __future__ import annotations

import json

from harmony_hub.update.launcher import MAX_TRIAL_ATTEMPTS, decide_release


def _write_state(root, state):
    path = root / "data" / "update_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _read_state(root):
    return json.loads((root / "data" / "update_state.json").read_text(encoding="utf-8"))


def test_a_fresh_root_with_nothing_deployed_has_no_release_to_boot(tmp_path):
    src, state = decide_release(tmp_path)
    assert src is None
    assert state == {}


def test_a_release_with_no_trial_boots_without_touching_attempts(tmp_path):
    _write_state(tmp_path, {"current": "build-1"})
    src, state = decide_release(tmp_path)

    assert src == str(tmp_path / "releases" / "build-1" / "src")
    assert state.get("trial") is None


def test_the_first_boot_of_a_trial_release_counts_as_one_attempt(tmp_path):
    _write_state(
        tmp_path,
        {"current": "build-2", "previous": "build-1", "trial": {"release": "build-2", "attempts": 0, "from": "build-1"}},
    )
    src, state = decide_release(tmp_path)

    assert src == str(tmp_path / "releases" / "build-2" / "src")
    assert state["trial"]["attempts"] == 1
    assert state["current"] == "build-2"  # still on trial, not rolled back yet


def test_attempts_below_the_limit_still_boot_the_trial_release(tmp_path):
    _write_state(
        tmp_path,
        {
            "current": "build-2",
            "previous": "build-1",
            "trial": {"release": "build-2", "attempts": MAX_TRIAL_ATTEMPTS - 1, "from": "build-1"},
        },
    )
    src, state = decide_release(tmp_path)

    assert state["current"] == "build-2"
    assert state["trial"]["attempts"] == MAX_TRIAL_ATTEMPTS
    assert src == str(tmp_path / "releases" / "build-2" / "src")


def test_a_release_that_exhausted_its_attempts_is_rolled_back_instead_of_tried_again(tmp_path):
    _write_state(
        tmp_path,
        {
            "current": "build-2",
            "previous": "build-1",
            "trial": {"release": "build-2", "attempts": MAX_TRIAL_ATTEMPTS, "from": "build-1"},
            "history": [],
        },
    )
    src, state = decide_release(tmp_path)

    assert state["current"] == "build-1"
    assert state["previous"] == "build-2"
    assert state["trial"] is None
    assert src == str(tmp_path / "releases" / "build-1" / "src")
    assert state["history"][-1] == {"build_id": "build-2", "installed_at": state["history"][-1]["installed_at"], "outcome": "rolled_back"}


def test_the_rollback_decision_is_persisted_to_disk(tmp_path):
    _write_state(
        tmp_path,
        {"current": "build-2", "previous": "build-1", "trial": {"release": "build-2", "attempts": MAX_TRIAL_ATTEMPTS, "from": "build-1"}},
    )
    decide_release(tmp_path)

    on_disk = _read_state(tmp_path)
    assert on_disk["current"] == "build-1"
    assert on_disk["trial"] is None


def test_a_release_with_no_recorded_from_rolls_back_to_no_release_at_all(tmp_path):
    """An edge case worth pinning down: a first-ever release that never boots has nothing to fall back to."""
    _write_state(
        tmp_path,
        {"current": "build-1", "previous": None, "trial": {"release": "build-1", "attempts": MAX_TRIAL_ATTEMPTS, "from": None}},
    )
    src, state = decide_release(tmp_path)
    assert state["current"] is None
    assert src is None


def test_a_corrupt_state_file_is_treated_as_a_fresh_start_not_a_crash(tmp_path):
    path = tmp_path / "data" / "update_state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    src, state = decide_release(tmp_path)
    assert src is None
    assert state == {}
