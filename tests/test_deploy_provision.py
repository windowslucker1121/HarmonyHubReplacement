"""Command construction and error handling for each provisioning step.

A fake `Connection` records what was asked of it rather than actually
executing anything -- this is about proving the *shape* of the commands
(quoting, sudo escalation, cleanup on both success and failure), not about
running against a real device.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pytest

from harmony_deploy import provision
from harmony_deploy.ssh import CommandResult
from harmony_hub.update.manifest import Manifest

ROOT = "/home/pi/harmony"


@dataclass
class FakeConnection:
    responses: Dict[str, CommandResult] = field(default_factory=dict)
    default: CommandResult = field(default_factory=lambda: CommandResult(0, "", ""))
    files: Dict[str, bytes] = field(default_factory=dict)
    puts: List[Tuple[str, str]] = field(default_factory=list)
    calls: List[Tuple[str, bool]] = field(default_factory=list)

    def run(self, command: str, *, timeout: float = 30.0) -> CommandResult:
        self.calls.append((command, False))
        return self.responses.get(command, self.default)

    def sudo(self, command: str, *, timeout: float = 60.0) -> CommandResult:
        self.calls.append((command, True))
        return self.responses.get(command, self.default)

    def put(self, local_path, remote_path: str) -> None:
        self.puts.append((str(local_path), remote_path))

    def put_bytes(self, data: bytes, remote_path: str) -> None:
        self.files[remote_path] = data

    def read_bytes(self, remote_path: str) -> Optional[bytes]:
        return self.files.get(remote_path)


def commands(conn: FakeConnection, sudo: Optional[bool] = None) -> List[str]:
    return [cmd for cmd, is_sudo in conn.calls if sudo is None or is_sudo == sudo]


def test_create_layout_makes_all_four_directories():
    conn = FakeConnection()
    provision.create_layout(conn, ROOT)
    assert commands(conn) == [f"mkdir -p {ROOT}/releases {ROOT}/incoming {ROOT}/data {ROOT}/bin"]


def test_create_venv_uses_system_site_packages():
    conn = FakeConnection()
    provision.create_venv(conn, ROOT)
    assert commands(conn) == [f"python3 -m venv --system-site-packages {ROOT}/venv"]


def test_install_pigpio_builds_from_source_rather_than_apt():
    """Not `apt install pigpio` -- Raspberry Pi OS stopped shipping it on
    Trixie/Bookworm, confirmed by an actual "no installation candidate" on
    a real device. See `provision.PIGPIO_REPO_URL`'s comment."""
    conn = FakeConnection()
    provision.install_pigpio(conn)

    all_commands = commands(conn)
    assert not any("apt-get install" in cmd and "pigpio " in cmd for cmd in all_commands)
    assert not any("python3-pigpio" in cmd for cmd in all_commands)
    assert any("git clone" in cmd and provision.PIGPIO_REPO_URL in cmd for cmd in all_commands)
    assert any(cmd.endswith("make -j4") for cmd in all_commands)


def test_install_pigpio_builds_and_clones_without_sudo_but_installs_with_it():
    conn = FakeConnection()
    provision.install_pigpio(conn)

    unprivileged = commands(conn, sudo=False)
    assert any("git clone" in cmd for cmd in unprivileged)
    assert any(cmd.endswith("make -j4") for cmd in unprivileged)

    privileged = commands(conn, sudo=True)
    assert any("apt-get install" in cmd and "build-essential" in cmd for cmd in privileged)
    assert any("make install" in cmd for cmd in privileged)


def test_install_pigpio_checks_the_binary_rather_than_make_installs_own_exit_code():
    """`make install`'s last step fails on every current Raspberry Pi OS
    (see the docstring) -- this must not treat that as a real failure as
    long as the binary that actually matters made it onto disk."""
    conn = FakeConnection(
        responses={
            f"cd {provision._PIGPIO_BUILD_DIR} && make install; test -x /usr/local/bin/pigpiod": CommandResult(
                0, "", ""
            ),
        },
    )
    provision.install_pigpio(conn)  # must not raise
    assert conn.files  # got as far as staging the systemd unit


def test_install_pigpio_raises_if_the_binary_never_actually_landed():
    conn = FakeConnection(
        responses={
            f"cd {provision._PIGPIO_BUILD_DIR} && make install; test -x /usr/local/bin/pigpiod": CommandResult(
                1, "", "test: /usr/local/bin/pigpiod: not found"
            ),
        },
    )
    with pytest.raises(provision.ProvisionError, match="pigpiod"):
        provision.install_pigpio(conn)


def test_install_pigpio_writes_and_enables_its_own_systemd_unit():
    conn = FakeConnection()
    provision.install_pigpio(conn)

    staged_path = next(remote for remote in conn.files if "pigpiod.service" in remote)
    assert conn.files[staged_path].decode() == provision.PIGPIOD_UNIT
    assert "ExecStart=/usr/local/bin/pigpiod" in provision.PIGPIOD_UNIT

    privileged = commands(conn, sudo=True)
    assert any("cp" in cmd and "pigpiod.service" in cmd and "systemctl daemon-reload" in cmd for cmd in privileged)
    assert "systemctl enable --now pigpiod" in privileged


def test_install_pigpio_reports_progress():
    conn = FakeConnection()
    messages = []
    provision.install_pigpio(conn, messages.append)
    assert len(messages) >= 3
    assert any("pigpio" in m.lower() for m in messages)


def test_migrate_config_moves_settings_and_credentials_best_effort():
    conn = FakeConnection()
    provision.migrate_config(conn, ROOT)
    (cmd,) = commands(conn)
    assert f"{ROOT}/hub_settings.json" in cmd
    assert f"{ROOT}/hub_config.json" in cmd
    assert f"{ROOT}/buttons.json" in cmd
    assert f"{ROOT}/credentials" in cmd
    assert f"{ROOT}/codes" in cmd
    assert f"{ROOT}/data/" in cmd
    assert "2>/dev/null" in cmd  # a device that never paired anything has no credentials/ -- not an error


def test_remove_stale_install_uninstalls_the_pip_package():
    conn = FakeConnection()
    provision.remove_stale_install(conn, ROOT)
    assert commands(conn) == [f"{ROOT}/venv/bin/pip uninstall -y harmony-receiver"]


def test_upload_launcher_puts_then_makes_it_executable(tmp_path):
    conn = FakeConnection()
    local = tmp_path / "launcher.py"
    local.write_text("print(1)")
    provision.upload_launcher(conn, ROOT, local)

    assert conn.puts == [(str(local), f"{ROOT}/bin/harmony-launch")]
    assert commands(conn) == [f"chmod 755 {ROOT}/bin/harmony-launch"]


def test_bootstrap_release_extracts_installs_deps_and_writes_current(tmp_path):
    conn = FakeConnection()
    tar_path = tmp_path / "build.tar.gz"
    tar_path.write_bytes(b"fake tar contents")
    emitted = []

    provision.bootstrap_release(conn, ROOT, tar_path, "build-1", emitted.append)

    assert conn.puts == [(str(tar_path), f"{ROOT}/incoming/build-1.tar.gz")]
    cmds = commands(conn)
    assert f"mkdir -p {ROOT}/releases/build-1" in cmds
    assert f"tar -xzf {ROOT}/incoming/build-1.tar.gz -C {ROOT}/releases/build-1" in cmds
    assert f"rm -f {ROOT}/incoming/build-1.tar.gz" in cmds
    assert any("pip install -r" in c and "build-1/requirements.txt" in c for c in cmds)
    assert json.loads(conn.files[f"{ROOT}/data/update_state.json"]) == {"current": "build-1"}
    assert any("Uploading" in msg for msg in emitted)


def _manifest(build_id: str = "build-2") -> Manifest:
    return Manifest(
        build_id=build_id, created_at="2026-08-26T00:00:00Z", file_count=1, byte_count=10, content_sha256="a" * 64
    )


def test_update_release_runs_the_current_releases_installer_not_the_new_ones(tmp_path):
    conn = FakeConnection()
    tar_path = tmp_path / "build-2.tar.gz"
    tar_path.write_bytes(b"fake tar contents")
    manifest = _manifest("build-2")

    provision.update_release(conn, ROOT, tar_path, manifest, current_build="build-1", emit=lambda _msg: None)

    assert (str(tar_path), f"{ROOT}/incoming/build-2.tar.gz") in conn.puts
    script_path = f"{ROOT}/incoming/_install_build-2.py"
    script = conn.files[script_path].decode("utf-8")
    assert "harmony_hub.update.installer import install" in script
    assert '"build-2"' in script  # the manifest's own build_id, embedded via its JSON

    run_cmd = next(cmd for cmd, is_sudo in conn.calls if "venv/bin/python" in cmd and not is_sudo)
    assert f"PYTHONPATH={ROOT}/releases/build-1/src" in run_cmd  # the OLD release's src, not the new one
    assert script_path in run_cmd

    assert f"rm -f {script_path} {ROOT}/incoming/build-2.tar.gz" in commands(conn)


def test_update_release_raises_on_a_failed_install_and_still_cleans_up(tmp_path):
    conn = FakeConnection(default=CommandResult(0, "", ""))
    tar_path = tmp_path / "build-2.tar.gz"
    tar_path.write_bytes(b"x")
    manifest = _manifest("build-2")

    run_cmd_marker = f"{ROOT}/venv/bin/python {ROOT}/incoming/_install_build-2.py"
    conn.responses[f"PYTHONPATH={ROOT}/releases/build-1/src {run_cmd_marker}"] = CommandResult(
        1, "", "ImportError: boom"
    )

    with pytest.raises(provision.ProvisionError, match="boom"):
        provision.update_release(conn, ROOT, tar_path, manifest, current_build="build-1", emit=lambda _msg: None)

    # Cleanup still ran even though the install itself failed.
    assert f"rm -f {ROOT}/incoming/_install_build-2.py {ROOT}/incoming/build-2.tar.gz" in commands(conn)


def test_write_systemd_unit_stages_then_copies_with_sudo():
    conn = FakeConnection()
    provision.write_systemd_unit(conn, ROOT, "pi")

    staged = f"{ROOT}/incoming/harmony-hub.service"
    assert staged in conn.files
    assert f"WorkingDirectory={ROOT}/data" in conn.files[staged].decode()

    (sudo_cmd,) = commands(conn, sudo=True)
    assert f"cp {staged} /etc/systemd/system/harmony-hub.service" in sudo_cmd
    assert "daemon-reload" in sudo_cmd
    assert commands(conn, sudo=False) == []  # nothing here should run unprivileged


def test_restart_service_enables_and_restarts_via_sudo():
    conn = FakeConnection()
    provision.restart_service(conn)
    assert commands(conn, sudo=True) == ["systemctl enable --now harmony-hub", "systemctl restart harmony-hub"]


def test_fetch_token_returns_the_raw_bytes():
    conn = FakeConnection(files={f"{ROOT}/data/update_token": b"\x00" * 32})
    assert provision.fetch_token(conn, ROOT) == b"\x00" * 32


def test_fetch_token_without_a_hub_ever_having_started_raises():
    conn = FakeConnection()
    with pytest.raises(provision.ProvisionError, match="ever started"):
        provision.fetch_token(conn, ROOT)


def test_a_failed_command_raises_with_the_remote_stderr():
    conn = FakeConnection(default=CommandResult(1, "", "permission denied"))
    with pytest.raises(provision.ProvisionError, match="permission denied"):
        provision.create_layout(conn, ROOT)
