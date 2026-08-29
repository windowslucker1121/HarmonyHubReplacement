"""Executes a plan against a real device over SSH.

Each `Step` from `plan.py` becomes one function here, in the same order
`build_plan` decided. Kept apart from `plan.py` so the *decision* of what
to do is tested without a network (`test_deploy_plan.py`) while this file
stays a straightforward translation of already-reviewed steps into
commands -- the part that actually touches a Pi, and the part with the
least room for a unit test to say anything a human reading it couldn't
already tell.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional

from harmony_hub.update.manifest import Manifest

from .plan import render_systemd_unit
from .ssh import Connection, quote

logger = logging.getLogger("harmony_deploy.provision")

Emit = Callable[[str], None]


class ProvisionError(RuntimeError):
    """A remote command failed. The message is the remote stderr/stdout, meant to be read as-is."""


def _run(conn: Connection, command: str, *, sudo: bool = False, timeout: float = 60.0) -> str:
    result = conn.sudo(command, timeout=timeout) if sudo else conn.run(command, timeout=timeout)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.exit_code}"
        raise ProvisionError(f"`{command}` failed: {detail}")
    return result.stdout


def create_layout(conn: Connection, root: str) -> None:
    _run(conn, f"mkdir -p {quote(root)}/releases {quote(root)}/incoming {quote(root)}/data {quote(root)}/bin")


def create_venv(conn: Connection, root: str) -> None:
    # `--system-site-packages`: matches the guidance in RASPBERRY_PI_DEPLOYMENT.md
    # for when `lgpio`'s C extension has to come from apt rather than pip.
    _run(conn, f"python3 -m venv --system-site-packages {quote(root)}/venv", timeout=120.0)


#: Raspberry Pi OS stopped packaging `pigpio` once Debian moved to
#: Trixie/Bookworm-based releases -- confirmed the hard way on a real
#: device, where `apt install pigpio` fails outright with "no installation
#: candidate." The upstream project is unmaintained but its C source still
#: builds and runs correctly on a Pi 3-family board -- the DMA/register
#: access `pigpiod` needs has not changed there, only Raspberry Pi Ltd's
#: choice to stop packaging it has.
PIGPIO_REPO_URL = "https://github.com/joan2937/pigpio.git"

#: `make install` alone does not ship this -- that used to come from the
#: `.deb` package -- so `install_pigpio` writes and enables it itself. No
#: per-device values to template: unlike harmony-hub's own unit, this one
#: is identical on every install, so it lives as a plain constant rather
#: than going through `plan.py`'s `render_systemd_unit`.
PIGPIOD_UNIT = """[Unit]
Description=pigpio daemon
After=network.target

[Service]
ExecStart=/usr/local/bin/pigpiod
ExecStop=/bin/systemctl kill pigpiod
Type=forking

[Install]
WantedBy=multi-user.target
"""

_PIGPIO_BUILD_DIR = "/tmp/pigpio-build"


def install_pigpio(conn: Connection, emit: Optional[Emit] = None) -> None:
    """Builds `pigpiod` from source, installs it, and enables it as a service.

    Not `apt install pigpio` -- see `PIGPIO_REPO_URL`'s comment for why that
    fails on every current Raspberry Pi OS. This is exactly the manual fix a
    real device needed after the IR backend shipped without it: the hub
    started fine and reported every other backend healthy, but every IR
    learn attempt failed with "pigpiod not reachable" until this was run by
    hand. Folding it into `setup` means a fresh device never hits that gap.

    `make install` on its own is deliberately not relied on for its exit
    code: its last step tries to install a bundled Python module via
    `distutils`, which Python 3.12+ dropped from the standard library, and
    which fails on every current Raspberry Pi OS -- on a system-wide module
    the hub never uses anyway, since its own venv gets `pigpio` from PyPI
    instead, where this problem does not exist. Rather than depend on that
    failing step's position staying the last one across future pigpio
    revisions, the command checks for the one file that actually matters
    (`/usr/local/bin/pigpiod`) afterwards, and only raises if that is
    missing -- `make install`'s own exit code is otherwise ignored.
    """

    def _emit(message: str) -> None:
        if emit is not None:
            emit(message)

    _emit("Installing build tools...")
    _run(conn, "apt-get update -qq && apt-get install -y -qq git build-essential", sudo=True, timeout=180.0)

    _emit("Fetching pigpio source...")
    _run(conn, f"rm -rf {quote(_PIGPIO_BUILD_DIR)}", sudo=True, timeout=30.0)
    _run(conn, f"git clone --depth 1 {PIGPIO_REPO_URL} {quote(_PIGPIO_BUILD_DIR)}", timeout=120.0)

    _emit("Building pigpio (this can take a minute)...")
    _run(conn, f"cd {quote(_PIGPIO_BUILD_DIR)} && make -j4", timeout=180.0)

    _emit("Installing pigpiod...")
    _run(
        conn,
        f"cd {quote(_PIGPIO_BUILD_DIR)} && make install; test -x /usr/local/bin/pigpiod",
        sudo=True,
        timeout=60.0,
    )
    _run(conn, f"rm -rf {quote(_PIGPIO_BUILD_DIR)}", sudo=True, timeout=30.0)

    staged = "/tmp/pigpiod.service"
    conn.put_bytes(PIGPIOD_UNIT.encode("utf-8"), staged)
    _run(
        conn,
        f"cp {quote(staged)} /etc/systemd/system/pigpiod.service && "
        f"rm -f {quote(staged)} && systemctl daemon-reload",
        sudo=True,
        timeout=30.0,
    )
    _emit("Enabling pigpiod...")
    _run(conn, "systemctl enable --now pigpiod", sudo=True, timeout=30.0)


def migrate_config(conn: Connection, root: str) -> None:
    """Moves the per-device files an update must never touch into `data/`.

    Best-effort per file (`2>/dev/null; true`) rather than one command that
    fails outright if, say, `credentials/` does not exist on a device that
    never paired anything -- that is a normal, not an error.
    """
    _run(
        conn,
        f"mv {quote(root)}/hub_settings.json {quote(root)}/hub_config.json {quote(root)}/buttons.json "
        f"{quote(root)}/data/ 2>/dev/null; "
        f"mv {quote(root)}/credentials {quote(root)}/captures {quote(root)}/codes {quote(root)}/data/ "
        "2>/dev/null; true",
    )


def remove_stale_install(conn: Connection, root: str) -> None:
    _run(conn, f"{quote(root)}/venv/bin/pip uninstall -y harmony-receiver", timeout=60.0)


def upload_launcher(conn: Connection, root: str, launcher_path: "Path | str") -> None:
    conn.put(launcher_path, f"{root}/bin/harmony-launch")
    _run(conn, f"chmod 755 {quote(root)}/bin/harmony-launch")


def bootstrap_release(conn: Connection, root: str, tar_path: "Path | str", build_id: str, emit: Emit) -> None:
    """The one time a release is installed without going through `installer.install` at all.

    There is no running release yet whose installer could do the job --
    this is the automated equivalent of the manual bootstrap steps in
    RASPBERRY_PI_DEPLOYMENT.md, not a shortcut around the smoke test an
    ordinary update gets. (A bootstrap that fails to import just fails to
    start, the same as it always has for a first install.)
    """
    tar_path = Path(tar_path)
    remote_tar = f"{root}/incoming/{build_id}.tar.gz"
    emit(f"Uploading {tar_path.stat().st_size} bytes...")
    conn.put(tar_path, remote_tar)

    release_dir = f"{root}/releases/{build_id}"
    _run(conn, f"mkdir -p {quote(release_dir)}")
    _run(conn, f"tar -xzf {quote(remote_tar)} -C {quote(release_dir)}")
    _run(conn, f"rm -f {quote(remote_tar)}")

    emit("Installing dependencies...")
    _run(conn, f"{quote(root)}/venv/bin/pip install -r {quote(release_dir)}/requirements.txt", timeout=600.0)

    _run(conn, f"mkdir -p {quote(root)}/data")
    state_json = json.dumps({"current": build_id})
    conn.put_bytes(state_json.encode("utf-8"), f"{root}/data/update_state.json")


def update_release(
    conn: Connection, root: str, tar_path: "Path | str", manifest: Manifest, current_build: str, emit: Emit
) -> None:
    """Uploads the new bundle and installs it through the *currently active* release's own installer.

    Deliberately not the incoming release's code: this is the same
    verify-stage-deps-smoke test-activate pipeline the HTTP path uses
    (`harmony_hub.update.installer.install`), just invoked over SSH instead
    of an HTTP request -- SSH replaces signature verification with "you
    already authenticated to get this far", nothing else about the pipeline
    changes. A bundle broken at import time still cannot be relied on to
    install itself, which is why the code that runs this is the release
    already known to work.
    """
    tar_path = Path(tar_path)
    remote_tar = f"{root}/incoming/{manifest.build_id}.tar.gz"
    emit(f"Uploading {tar_path.stat().st_size} bytes...")
    conn.put(tar_path, remote_tar)

    script = (
        "import asyncio\n"
        "from harmony_hub.update.installer import install\n"
        "from harmony_hub.update.manifest import Manifest\n"
        f"manifest = Manifest.model_validate_json({manifest.model_dump_json()!r})\n"
        f"asyncio.run(install({root!r}, {remote_tar!r}, manifest))\n"
    )
    remote_script = f"{root}/incoming/_install_{manifest.build_id}.py"
    conn.put_bytes(script.encode("utf-8"), remote_script)

    pythonpath = f"{root}/releases/{current_build}/src"
    emit("Installing dependencies and running the smoke test on the device...")
    result = conn.run(
        f"PYTHONPATH={quote(pythonpath)} {quote(root)}/venv/bin/python {quote(remote_script)}",
        timeout=900.0,
    )
    _run(conn, f"rm -f {quote(remote_script)} {quote(remote_tar)}")
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ProvisionError(f"install failed on the device: {detail}")


def write_systemd_unit(conn: Connection, root: str, user: str) -> None:
    unit_text = render_systemd_unit(root, user)
    staged = f"{root}/incoming/harmony-hub.service"
    conn.put_bytes(unit_text.encode("utf-8"), staged)
    _run(
        conn,
        f"cp {quote(staged)} /etc/systemd/system/harmony-hub.service && "
        f"rm -f {quote(staged)} && systemctl daemon-reload",
        sudo=True,
        timeout=30.0,
    )


def restart_service(conn: Connection) -> None:
    _run(conn, "systemctl enable --now harmony-hub", sudo=True, timeout=30.0)
    _run(conn, "systemctl restart harmony-hub", sudo=True, timeout=30.0)


def fetch_token(conn: Connection, root: str) -> bytes:
    data = conn.read_bytes(f"{root}/data/update_token")
    if data is None:
        raise ProvisionError(f"{root}/data/update_token was not found -- has the hub ever started successfully?")
    return data
