"""Turning raw command output into a DeviceState. No SSH, no paramiko -- just a fake Runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

import pytest

from harmony_deploy.probe import CannotResolveRoot, inspect, resolve_remote_path
from harmony_deploy.ssh import CommandResult

ROOT = "/home/pi/harmony"


@dataclass
class FakeConnection:
    """Maps exact command strings to canned results. An unlisted command is treated as `exit 1`."""

    responses: Dict[str, CommandResult] = field(default_factory=dict)
    files: Dict[str, str] = field(default_factory=dict)

    def run(self, command: str, *, timeout: float = 30.0) -> CommandResult:
        return self.responses.get(command, CommandResult(1, "", ""))

    def sudo(self, command: str, *, timeout: float = 60.0) -> CommandResult:
        return self.run(command)

    def read_file(self, remote_path: str) -> Optional[str]:
        return self.files.get(remote_path)


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(0, stdout, "")


def fail() -> CommandResult:
    return CommandResult(1, "", "")


def test_a_bare_device_reports_nothing_present():
    conn = FakeConnection()
    state = inspect(conn, ROOT)

    assert state.venv_exists is False
    assert state.releases_dir_exists is False
    assert state.releases == []
    assert state.current_release is None
    assert state.stale_editable_install is False
    assert state.config_at_old_location is False
    assert state.config_at_new_location is False
    assert state.launcher_sha256 is None
    assert state.systemd_unit_content is None
    assert state.service_active is False
    assert state.pigpiod_enabled is False
    assert state.passwordless_sudo is False


def test_a_fully_provisioned_device_is_read_back_correctly():
    conn = FakeConnection(
        responses={
            "uname -m": ok("aarch64"),
            "python3 --version": ok("Python 3.11.2"),
            f"df -Pk {ROOT} 2>/dev/null | tail -1": ok("/dev/root 30000000 5000000 3200000 61% /"),
            f"test -d {ROOT}": ok(),
            f"test -e {ROOT}/venv/bin/python": ok(),
            f"test -d {ROOT}/releases": ok(),
            f"test -d {ROOT}/data": ok(),
            f"test -d {ROOT}/bin": ok(),
            f"test -e {ROOT}/venv/bin/harmony-hub": fail(),
            f"test -e {ROOT}/hub_settings.json": fail(),
            f"test -e {ROOT}/data/hub_settings.json": ok(),
            f"ls -1 {ROOT}/releases 2>/dev/null": ok("20260101T000000-old\n20260826T101500-a1b2c3d\n"),
            f"sha256sum {ROOT}/bin/harmony-launch 2>/dev/null": ok(f"cafebabe  {ROOT}/bin/harmony-launch\n"),
            "systemctl cat harmony-hub 2>/dev/null": ok("# unit file\n[Service]\nExecStart=/x\n"),
            "systemctl is-active --quiet harmony-hub": ok(),
            "systemctl is-enabled --quiet harmony-hub": ok(),
            "systemctl is-enabled --quiet pigpiod": ok(),
            "sudo -n true": ok(),
        },
        files={
            f"{ROOT}/data/update_state.json": (
                '{"current": "20260826T101500-a1b2c3d", "previous": "20260101T000000-old", '
                '"trial": null, "deps_hash": "deadbeef"}'
            ),
        },
    )

    state = inspect(conn, ROOT)

    assert state.arch == "aarch64"
    assert state.python_version == "Python 3.11.2"
    assert state.free_bytes == 3200000 * 1024
    assert state.root_exists is True
    assert state.venv_exists is True
    assert state.releases_dir_exists is True
    assert state.data_dir_exists is True
    assert state.bin_dir_exists is True
    assert state.releases == ["20260101T000000-old", "20260826T101500-a1b2c3d"]
    assert state.current_release == "20260826T101500-a1b2c3d"
    assert state.previous_release == "20260101T000000-old"
    assert state.trial_active is False
    assert state.deps_hash == "deadbeef"
    assert state.config_at_old_location is False
    assert state.config_at_new_location is True
    assert state.stale_editable_install is False
    assert state.launcher_sha256 == "cafebabe"
    assert state.systemd_unit_content == "# unit file\n[Service]\nExecStart=/x\n"
    assert state.service_active is True
    assert state.service_enabled is True
    assert state.pigpiod_enabled is True
    assert state.passwordless_sudo is True


def test_a_trial_release_is_reported_as_active():
    conn = FakeConnection(
        files={
            f"{ROOT}/data/update_state.json": (
                '{"current": "build-2", "previous": "build-1", '
                '"trial": {"release": "build-2", "attempts": 1, "from": "build-1"}}'
            ),
        }
    )
    state = inspect(conn, ROOT)
    assert state.trial_active is True


def test_a_corrupt_update_state_file_does_not_crash_the_probe():
    conn = FakeConnection(files={f"{ROOT}/data/update_state.json": "{not valid json"})
    state = inspect(conn, ROOT)
    assert state.current_release is None


def test_no_installed_unit_reports_none_rather_than_empty_string():
    conn = FakeConnection(responses={"systemctl cat harmony-hub 2>/dev/null": fail()})
    state = inspect(conn, ROOT)
    assert state.systemd_unit_content is None


def test_an_empty_but_successful_unit_query_still_reports_none():
    """`systemctl cat` on a masked unit can exit 0 with nothing useful printed."""
    conn = FakeConnection(responses={"systemctl cat harmony-hub 2>/dev/null": ok("")})
    state = inspect(conn, ROOT)
    assert state.systemd_unit_content is None


# ---------------------------------------------------------------------------
# Resolving `~` before any path is used
# ---------------------------------------------------------------------------


def test_a_tilde_root_is_expanded_against_the_devices_real_home():
    """The regression this guards: every path goes out `shlex.quote`d, and

    quoting is exactly what stops a remote shell expanding `~`. A root of
    `~/harmony` sent as `test -d '~/harmony'` asks about a directory
    *literally named* `~` and is always false -- so a fully provisioned
    device reported as completely bare. SFTP and systemd do not expand `~`
    either, so resolving it once up front is the only thing that fixes all
    three at the same time.
    """
    conn = FakeConnection(responses={'printf %s "$HOME"': ok("/home/pi")})
    assert resolve_remote_path(conn, "~/harmony") == "/home/pi/harmony"


def test_a_bare_tilde_resolves_to_the_home_directory_itself():
    conn = FakeConnection(responses={'printf %s "$HOME"': ok("/home/pi")})
    assert resolve_remote_path(conn, "~") == "/home/pi"


def test_an_absolute_path_is_left_alone_and_costs_no_remote_command():
    conn = FakeConnection()
    assert resolve_remote_path(conn, "/opt/harmony") == "/opt/harmony"


def test_a_trailing_slash_is_normalised_away_so_paths_never_double_up():
    conn = FakeConnection(responses={'printf %s "$HOME"': ok("/home/pi/")})
    assert resolve_remote_path(conn, "~/harmony/") == "/home/pi/harmony"
    assert resolve_remote_path(FakeConnection(), "/opt/harmony/") == "/opt/harmony"


def test_another_users_home_is_refused_rather_than_guessed():
    conn = FakeConnection(responses={'printf %s "$HOME"': ok("/home/pi")})
    with pytest.raises(CannotResolveRoot, match="another user"):
        resolve_remote_path(conn, "~someoneelse/harmony")


def test_a_device_that_will_not_report_home_is_an_error_not_a_silent_literal_tilde():
    conn = FakeConnection(responses={'printf %s "$HOME"': fail()})
    with pytest.raises(CannotResolveRoot, match=r"\$HOME"):
        resolve_remote_path(conn, "~/harmony")


def test_free_space_survives_df_wrapping_a_long_device_name():
    """`df -Pk` (POSIX output) keeps each filesystem on one line.

    Plain `df -k` wraps a long device name onto its own line, and `tail -1`
    then reads a row whose columns are shifted -- which would report the
    use percentage as the free byte count.
    """
    conn = FakeConnection(
        responses={
            f"df -Pk {ROOT} 2>/dev/null | tail -1": ok(
                "/dev/disk/by-uuid/a-very-long-device-name 30000000 5000000 3200000 61% /"
            ),
        }
    )
    state = inspect(conn, ROOT)
    assert state.free_bytes == 3200000 * 1024
