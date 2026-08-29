"""What to do to a device, given what it already looks like.

Pure decision logic -- no SSH, no filesystem beyond a fixture's own
directory. `provision.py` is what actually runs these steps; this file only
covers the choice of *which* steps, table-style: one `DeviceState` in, one
expected step list out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harmony_deploy.plan import (
    AmbiguousConfigLocation,
    Step,
    StepKind,
    build_plan,
    local_launcher_sha256,
    render_systemd_unit,
    unit_matches,
)
from harmony_deploy.probe import DeviceState

ROOT = "/home/pi/harmony"
USER = "pi"
BUILD_ID = "20260826T101500-a1b2c3d"
DEPS_HASH = "deadbeef" * 8
LAUNCHER_HASH = "cafebabe" * 8


def bare_device() -> DeviceState:
    """Nothing exists yet -- a factory-fresh Pi with just an OS and network."""
    return DeviceState(root=ROOT)


def fully_provisioned(**overrides) -> DeviceState:
    """A device already on `BUILD_ID`, unit installed and matching, service running."""
    state = DeviceState(
        root=ROOT,
        venv_exists=True,
        releases_dir_exists=True,
        data_dir_exists=True,
        bin_dir_exists=True,
        config_at_new_location=True,
        releases=[BUILD_ID],
        current_release=BUILD_ID,
        launcher_sha256=LAUNCHER_HASH,
        systemd_unit_content=render_systemd_unit(ROOT, USER),
        service_active=True,
        service_enabled=True,
        pigpiod_enabled=True,
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def kinds(steps) -> list:
    return [step.kind for step in steps]


def plan_for(state: DeviceState, **kwargs) -> list:
    return build_plan(
        state,
        build_id=BUILD_ID,
        user=USER,
        deps_hash=DEPS_HASH,
        launcher_sha256=LAUNCHER_HASH,
        **kwargs,
    )


def test_a_bare_device_gets_the_full_provision_in_order():
    steps = plan_for(bare_device())

    assert kinds(steps) == [
        StepKind.CREATE_LAYOUT,
        StepKind.CREATE_VENV,
        StepKind.INSTALL_PIGPIO,
        StepKind.UPLOAD_LAUNCHER,
        StepKind.BOOTSTRAP_RELEASE,
        StepKind.WRITE_SYSTEMD_UNIT,
        StepKind.RESTART_SERVICE,
        StepKind.VERIFY,
        StepKind.FETCH_TOKEN,
    ]
    # No config anywhere yet, so nothing to migrate and nothing ambiguous.
    assert StepKind.MIGRATE_CONFIG not in kinds(steps)


def test_a_device_already_on_the_target_build_only_verifies_and_refreshes_the_token():
    steps = plan_for(fully_provisioned())
    assert kinds(steps) == [StepKind.VERIFY, StepKind.FETCH_TOKEN]


def test_a_device_on_an_older_build_updates_through_the_running_installer():
    state = fully_provisioned(releases=["20260101T000000-old"], current_release="20260101T000000-old")
    steps = plan_for(state)

    assert kinds(steps) == [
        StepKind.UPDATE_RELEASE,
        StepKind.RESTART_SERVICE,
        StepKind.VERIFY,
        StepKind.FETCH_TOKEN,
    ]


def test_config_still_at_the_old_location_is_migrated():
    state = fully_provisioned(config_at_new_location=False, config_at_old_location=True)
    steps = plan_for(state)
    assert StepKind.MIGRATE_CONFIG in kinds(steps)


def test_config_at_both_locations_refuses_to_guess():
    state = fully_provisioned(config_at_new_location=True, config_at_old_location=True)
    with pytest.raises(AmbiguousConfigLocation):
        plan_for(state)


def test_a_stale_editable_install_is_removed():
    state = fully_provisioned(stale_editable_install=True)
    steps = plan_for(state)
    assert StepKind.REMOVE_STALE_INSTALL in kinds(steps)


def test_a_correct_unit_and_running_service_need_no_restart():
    steps = plan_for(fully_provisioned())
    assert StepKind.WRITE_SYSTEMD_UNIT not in kinds(steps)
    assert StepKind.RESTART_SERVICE not in kinds(steps)


def test_force_unit_rewrite_adds_the_rewrite_and_a_restart_even_if_nothing_else_changed():
    steps = plan_for(fully_provisioned(), force_unit_rewrite=True)
    assert StepKind.WRITE_SYSTEMD_UNIT in kinds(steps)
    assert StepKind.RESTART_SERVICE in kinds(steps)


def test_a_stopped_service_is_restarted_even_if_the_build_and_unit_are_current():
    state = fully_provisioned(service_active=False)
    steps = plan_for(state)
    assert StepKind.RESTART_SERVICE in kinds(steps)


def test_an_outdated_unit_is_rewritten():
    state = fully_provisioned(systemd_unit_content="[Service]\nExecStart=/old/path\n")
    steps = plan_for(state)
    assert StepKind.WRITE_SYSTEMD_UNIT in kinds(steps)


def test_a_missing_launcher_is_uploaded():
    state = fully_provisioned(launcher_sha256=None)
    steps = plan_for(state)
    assert StepKind.UPLOAD_LAUNCHER in kinds(steps)


def test_a_matching_launcher_is_not_reuploaded():
    steps = plan_for(fully_provisioned())
    assert StepKind.UPLOAD_LAUNCHER not in kinds(steps)


def test_pigpiod_not_yet_enabled_is_installed():
    state = fully_provisioned(pigpiod_enabled=False)
    steps = plan_for(state)
    assert StepKind.INSTALL_PIGPIO in kinds(steps)


def test_pigpiod_already_enabled_needs_no_step():
    steps = plan_for(fully_provisioned())
    assert StepKind.INSTALL_PIGPIO not in kinds(steps)


def test_installing_pigpiod_needs_sudo():
    state = fully_provisioned(pigpiod_enabled=False)
    step = next(s for s in plan_for(state) if s.kind is StepKind.INSTALL_PIGPIO)
    assert step.needs_sudo is True


def test_local_launcher_hash_matches_a_manual_recompute(tmp_path):
    launcher = tmp_path / "launcher.py"
    launcher.write_bytes(b"print('hello')\n")
    import hashlib

    assert local_launcher_sha256(launcher) == hashlib.sha256(b"print('hello')\n").hexdigest()


class TestUnitMatches:
    def test_a_freshly_rendered_unit_matches_itself(self):
        assert unit_matches(render_systemd_unit(ROOT, USER), ROOT, USER)

    def test_systemctl_cats_own_header_comment_does_not_break_the_match(self):
        rendered = render_systemd_unit(ROOT, USER)
        wrapped = f"# /etc/systemd/system/harmony-hub.service\n{rendered}"
        assert unit_matches(wrapped, ROOT, USER)

    def test_none_never_matches(self):
        assert not unit_matches(None, ROOT, USER)

    def test_empty_string_never_matches(self):
        assert not unit_matches("", ROOT, USER)

    def test_a_different_root_does_not_match(self):
        assert not unit_matches(render_systemd_unit(ROOT, USER), "/home/pi/other", USER)

    def test_a_different_user_does_not_match(self):
        assert not unit_matches(render_systemd_unit(ROOT, USER), ROOT, "someoneelse")

    def test_the_pre_update_unit_shape_does_not_match(self):
        old_style = (
            "[Service]\n"
            f"WorkingDirectory={ROOT}\n"
            f"ExecStart={ROOT}/venv/bin/harmony-hub\n"
            "Restart=on-failure\n"
        )
        assert not unit_matches(old_style, ROOT, USER)
