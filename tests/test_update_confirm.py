"""Confirming a trial from inside the process that release is running."""

from __future__ import annotations

import asyncio

from harmony_hub.update import state as state_module
from harmony_hub.update.confirm import confirm_trial, schedule_confirmation, state_path


def test_confirming_a_trial_clears_it_and_saves(tmp_path):
    root = tmp_path
    initial = state_module.activate(state_module.UpdateState(current="build-1"), "build-2")
    state_module.save(initial, state_path(root))

    changed = confirm_trial(root)

    assert changed is True
    on_disk = state_module.load(state_path(root))
    assert on_disk.trial is None
    assert on_disk.history[-1].outcome == "good"


def test_confirming_with_nothing_on_trial_does_nothing(tmp_path):
    state_module.save(state_module.UpdateState(current="build-1"), state_path(tmp_path))
    assert confirm_trial(tmp_path) is False


def test_confirming_a_root_with_no_state_file_at_all_is_a_no_op(tmp_path):
    assert confirm_trial(tmp_path) is False


def test_schedule_confirmation_waits_then_confirms(tmp_path):
    initial = state_module.activate(state_module.UpdateState(current="build-1"), "build-2")
    state_module.save(initial, state_path(tmp_path))

    asyncio.run(schedule_confirmation(tmp_path, delay=0))

    assert state_module.load(state_path(tmp_path)).trial is None
