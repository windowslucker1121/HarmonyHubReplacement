"""run_setup end to end, with the network and the bundle build faked out.

Proves the orchestration wiring itself -- connect, inspect, plan, execute,
verify, fetch the token, write deploy_targets.json -- without a real
device, real SSH, or a real pytest+flutter build running inside this test.
Each piece it calls into (plan.build_plan, provision.*, probe.inspect) has
its own focused tests elsewhere; this one is about the glue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pytest

from harmony_deploy import setup as deploy_setup
from harmony_deploy.ssh import CommandResult
from harmony_hub.update.manifest import Manifest

ROOT = "/home/pi/harmony"


@dataclass
class FakeConnection:
    """A whole fake device: commands, files, and what got uploaded -- in one object."""

    responses: Dict[str, CommandResult] = field(default_factory=dict)
    #: What an unlisted *mutating* command returns -- success, so a bare-device
    #: bootstrap test does not have to enumerate every `mkdir`/`pip install`/
    #: `systemctl` it expects to run. An unlisted `test -e`/`test -d` is
    #: handled separately in `run()` below and always defaults to "not
    #: found" instead, regardless of this -- a bare device has nothing on it,
    #: and a provisioned-device test lists every existence check it cares
    #: about explicitly rather than relying on either default.
    default: CommandResult = field(default_factory=lambda: CommandResult(0, "", ""))
    files: Dict[str, bytes] = field(default_factory=dict)
    puts: List[Tuple[str, str]] = field(default_factory=list)
    closed: bool = False

    def run(self, command: str, *, timeout: float = 30.0) -> CommandResult:
        if command in self.responses:
            return self.responses[command]
        if command.startswith("test -e ") or command.startswith("test -d "):
            return CommandResult(1, "", "")
        return self.default

    def sudo(self, command: str, *, timeout: float = 60.0) -> CommandResult:
        return self.run(command)

    def read_file(self, remote_path: str) -> Optional[str]:
        data = self.files.get(remote_path)
        return None if data is None else data.decode("utf-8")

    def read_bytes(self, remote_path: str) -> Optional[bytes]:
        return self.files.get(remote_path)

    def put(self, local_path, remote_path: str) -> None:
        self.puts.append((str(local_path), remote_path))
        self.files[remote_path] = b"(uploaded file contents not tracked)"

    def put_bytes(self, data: bytes, remote_path: str) -> None:
        self.files[remote_path] = data

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def fake_manifest() -> Manifest:
    return Manifest(
        build_id="20260826T101500-a1b2c3d",
        created_at="2026-08-26T10:15:00Z",
        deps_hash="deadbeef",
        file_count=3,
        byte_count=100,
        content_sha256="a" * 64,
    )


@pytest.fixture
def fake_bundle(tmp_path, monkeypatch):
    manifest = fake_manifest()
    tar_path = tmp_path / f"{manifest.build_id}.tar.gz"
    tar_path.write_bytes(b"fake tar")
    monkeypatch.setattr(deploy_setup, "build_release_bundle", lambda *a, **k: (manifest, tar_path))
    return manifest, tar_path


@pytest.fixture
def no_http_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_setup, "wait_for_version", lambda url, build_id: calls.append((url, build_id)))
    return calls


def test_dry_run_inspects_and_plans_but_touches_nothing(fake_bundle, no_http_wait, tmp_path, monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(deploy_setup, "connect", lambda *a, **k: conn)

    deploy_setup.run_setup(
        tmp_path, "pi", host="10.0.0.1", user="pi", root=ROOT, dry_run=True, assume_yes=True
    )

    assert conn.puts == []  # nothing uploaded
    assert no_http_wait == []  # never even tried to verify
    assert not (tmp_path / "deploy_targets.json").exists()


def test_bootstrapping_a_bare_device_end_to_end(fake_bundle, no_http_wait, tmp_path, monkeypatch):
    manifest, tar_path = fake_bundle
    # The token file itself is created by the *real* hub process on this
    # device once it actually starts (see api.py's get_version) -- nothing
    # in this fake runs that process, so it is seeded here to stand in for
    # "the device restarted and came up" between the restart step and the
    # token fetch that follows it.
    conn = FakeConnection(files={f"{ROOT}/data/update_token": b"\x00" * 32})
    monkeypatch.setattr(deploy_setup, "connect", lambda *a, **k: conn)

    targets_file = tmp_path / "deploy_targets.json"
    token_dir = tmp_path / "tokens"

    deploy_setup.run_setup(
        tmp_path,
        "pi",
        host="10.0.0.1",
        user="pi",
        root=ROOT,
        assume_yes=True,
        targets_file=targets_file,
        token_dir=token_dir,
    )

    # The release actually landed and was activated.
    assert json.loads(conn.files[f"{ROOT}/data/update_state.json"]) == {"current": manifest.build_id}
    # The launcher and the systemd unit both went out.
    assert any(remote == f"{ROOT}/bin/harmony-launch" for _local, remote in conn.puts)
    assert f"{ROOT}/incoming/harmony-hub.service" in conn.files
    # The service was (re)started.
    assert conn.run("true") == conn.default  # sanity: fake still answers
    # Verification happened, against the address just connected to.
    assert no_http_wait == [(f"http://10.0.0.1:8765", manifest.build_id)]
    # The connection was closed no matter what.
    assert conn.closed is True

    # And the whole point: a token came back, and push() now has what it needs.
    token_path = token_dir / "pi.token"
    assert token_path.is_file()
    saved = json.loads(targets_file.read_text())
    assert saved["pi"]["url"] == "http://10.0.0.1:8765"
    assert saved["pi"]["token_file"] == str(token_path)


def test_pigpiod_is_installed_when_not_yet_enabled(fake_bundle, no_http_wait, tmp_path, monkeypatch):
    """The exact gap a real device fell into: the IR backend shipped, but
    nothing installed the daemon it needs, and the only fix was running it
    by hand. `setup` now closes that gap on a fresh device automatically."""
    conn = FakeConnection(
        responses={"systemctl is-enabled --quiet pigpiod": CommandResult(1, "", "")},
        files={f"{ROOT}/data/update_token": b"\x00" * 32},
    )
    monkeypatch.setattr(deploy_setup, "connect", lambda *a, **k: conn)
    calls = []
    monkeypatch.setattr(deploy_setup.provision, "install_pigpio", lambda c, emit: calls.append(c))

    deploy_setup.run_setup(
        tmp_path,
        "pi",
        host="10.0.0.1",
        user="pi",
        root=ROOT,
        assume_yes=True,
        targets_file=tmp_path / "deploy_targets.json",
        token_dir=tmp_path / "tokens",
    )

    assert calls == [conn]


def test_pigpiod_already_enabled_is_not_reinstalled(fake_bundle, no_http_wait, tmp_path, monkeypatch):
    conn = FakeConnection(files={f"{ROOT}/data/update_token": b"\x00" * 32})
    monkeypatch.setattr(deploy_setup, "connect", lambda *a, **k: conn)
    calls = []
    monkeypatch.setattr(deploy_setup.provision, "install_pigpio", lambda c, emit: calls.append(c))

    deploy_setup.run_setup(
        tmp_path,
        "pi",
        host="10.0.0.1",
        user="pi",
        root=ROOT,
        assume_yes=True,
        targets_file=tmp_path / "deploy_targets.json",
        token_dir=tmp_path / "tokens",
    )

    assert calls == []


def test_declining_confirmation_changes_nothing(fake_bundle, no_http_wait, tmp_path, monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(deploy_setup, "connect", lambda *a, **k: conn)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    deploy_setup.run_setup(tmp_path, "pi", host="10.0.0.1", user="pi", root=ROOT, assume_yes=False)

    assert conn.puts == []
    assert no_http_wait == []


def test_a_device_already_up_to_date_only_verifies_and_refreshes_the_token(fake_bundle, no_http_wait, tmp_path, monkeypatch):
    from harmony_deploy.plan import render_systemd_unit
    from harmony_deploy.plan import local_launcher_sha256

    manifest, tar_path = fake_bundle
    launcher_hash = local_launcher_sha256(deploy_setup.LAUNCHER_PATH)
    conn = FakeConnection(
        responses={
            f"test -d {ROOT}": CommandResult(0, "", ""),
            f"test -e {ROOT}/venv/bin/python": CommandResult(0, "", ""),
            f"test -d {ROOT}/releases": CommandResult(0, "", ""),
            f"test -d {ROOT}/data": CommandResult(0, "", ""),
            f"test -d {ROOT}/bin": CommandResult(0, "", ""),
            f"test -e {ROOT}/data/hub_settings.json": CommandResult(0, "", ""),
            f"ls -1 {ROOT}/releases 2>/dev/null": CommandResult(0, f"{manifest.build_id}\n", ""),
            f"sha256sum {ROOT}/bin/harmony-launch 2>/dev/null": CommandResult(0, f"{launcher_hash}  x\n", ""),
            "systemctl cat harmony-hub 2>/dev/null": CommandResult(0, render_systemd_unit(ROOT, "pi"), ""),
            "systemctl is-active --quiet harmony-hub": CommandResult(0, "", ""),
            "systemctl is-enabled --quiet harmony-hub": CommandResult(0, "", ""),
        },
        files={
            f"{ROOT}/data/update_state.json": json.dumps({"current": manifest.build_id}).encode(),
            f"{ROOT}/data/update_token": b"\x00" * 32,
        },
    )
    monkeypatch.setattr(deploy_setup, "connect", lambda *a, **k: conn)

    deploy_setup.run_setup(
        tmp_path,
        "pi",
        host="10.0.0.1",
        user="pi",
        root=ROOT,
        assume_yes=True,
        targets_file=tmp_path / "deploy_targets.json",
        token_dir=tmp_path / "tokens",
    )

    assert conn.puts == []  # nothing needed uploading
    assert no_http_wait == [("http://10.0.0.1:8765", manifest.build_id)]
