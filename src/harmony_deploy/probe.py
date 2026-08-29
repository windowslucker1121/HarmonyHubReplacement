"""Reads a device's current state without changing anything on it.

Each check here is one cheap remote command; the interesting part is
turning that raw output into a `DeviceState` a planner can reason about.
Depends only on `ssh.Runner` -- not `ssh.Connection` itself, and never
`paramiko` -- so this is testable against a plain fake with no network at
all. `plan.py` is where a `DeviceState` turns into decisions; this module
only observes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

from .ssh import Runner, quote


@dataclass
class DeviceState:
    root: str

    arch: str = ""
    python_version: str = ""
    free_bytes: int = 0

    root_exists: bool = False
    venv_exists: bool = False
    releases_dir_exists: bool = False
    data_dir_exists: bool = False
    bin_dir_exists: bool = False

    #: Directory names under `releases/`, whatever they are -- `plan.py`
    #: decides what a given name implies, this just reports what is there.
    releases: List[str] = field(default_factory=list)

    current_release: Optional[str] = None
    previous_release: Optional[str] = None
    trial_active: bool = False
    #: The dependency hash last recorded as installed, from
    #: `update_state.json` -- compared against a new bundle's own hash to
    #: decide whether `pip install` needs to run again.
    deps_hash: Optional[str] = None

    config_at_old_location: bool = False
    config_at_new_location: bool = False
    stale_editable_install: bool = False

    #: `None` when `bin/harmony-launch` does not exist yet.
    launcher_sha256: Optional[str] = None

    #: Raw `systemctl cat` output, or `None` if no unit is installed (or it
    #: could not be read). `plan.unit_matches` is what interprets this.
    systemd_unit_content: Optional[str] = None
    service_active: bool = False
    service_enabled: bool = False

    #: Whether `pigpiod` -- the daemon the IR backend needs to time its
    #: transmit waveform and captures -- is enabled. Installing the
    #: `pigpio`/`python3-pigpio` packages also enables and starts it in one
    #: step (`systemctl enable --now`), so "enabled" alone is what
    #: `plan.py` uses to decide the whole thing is already done; there is
    #: no separate "installed but not enabled" state this tool would ever
    #: itself produce.
    pigpiod_enabled: bool = False

    passwordless_sudo: bool = False


class CannotResolveRoot(RuntimeError):
    """The install root could not be turned into an absolute path on the device."""


def resolve_remote_path(conn: Runner, path: str) -> str:
    """Expands a leading `~` against the device's real home directory.

    Every path this tool sends to the device goes out `shlex.quote`d, which
    is right for a path containing spaces and wrong for one containing a
    tilde: quoting is exactly what stops the remote shell expanding `~`, so
    `test -d '~/harmony'` asks about a directory *literally named* `~` and
    is always false. SFTP does not expand `~` either, and neither does
    systemd -- an `ExecStart=~/harmony/...` unit simply never starts.

    Rather than special-case quoting per call site, the tilde is resolved
    once, here, before any path is used: everything downstream then deals
    only in absolute paths.
    """
    if not path.startswith("~"):
        return path.rstrip("/") or "/"

    if path != "~" and not path.startswith("~/"):
        # `~someone-else/harmony`. Resolving it means asking the device
        # about another account's home, which is a different question than
        # this tool has any business answering silently.
        raise CannotResolveRoot(
            f"{path!r} refers to another user's home directory -- give an absolute path instead"
        )

    result = conn.run('printf %s "$HOME"')
    home = result.stdout.strip()
    if not result.ok or not home:
        raise CannotResolveRoot(
            "could not read $HOME on the device to expand '~' -- give an absolute path instead"
        )
    return (home.rstrip("/") + path[1:]).rstrip("/") or "/"


def inspect(conn: Runner, root: str) -> DeviceState:
    """Reads device state. `root` must already be absolute -- see `resolve_remote_path`."""
    state = DeviceState(root=root)

    uname = conn.run("uname -m")
    state.arch = uname.stdout.strip() if uname.ok else ""

    py = conn.run("python3 --version")
    state.python_version = (py.stdout or py.stderr).strip()

    # `-P` (POSIX output) rather than plain `-k`: without it, df wraps a
    # long device name onto its own line, and `tail -1` then reads a row
    # whose columns are shifted one to the left -- reporting the *use
    # percentage* as free bytes.
    df = conn.run(f"df -Pk {quote(root)} 2>/dev/null | tail -1")
    fields = df.stdout.split()
    if df.ok and len(fields) >= 4:
        try:
            state.free_bytes = int(fields[3]) * 1024
        except ValueError:
            pass

    state.root_exists = _is_dir(conn, root)
    state.venv_exists = _exists(conn, f"{root}/venv/bin/python")
    state.releases_dir_exists = _is_dir(conn, f"{root}/releases")
    state.data_dir_exists = _is_dir(conn, f"{root}/data")
    state.bin_dir_exists = _is_dir(conn, f"{root}/bin")

    state.stale_editable_install = _exists(conn, f"{root}/venv/bin/harmony-hub")
    state.config_at_old_location = _exists(conn, f"{root}/hub_settings.json")
    state.config_at_new_location = _exists(conn, f"{root}/data/hub_settings.json")

    releases = conn.run(f"ls -1 {quote(root + '/releases')} 2>/dev/null")
    state.releases = [line.strip() for line in releases.stdout.splitlines() if line.strip()]

    raw_state = conn.read_file(f"{root}/data/update_state.json")
    if raw_state:
        try:
            parsed = json.loads(raw_state)
        except ValueError:
            parsed = {}
        state.current_release = parsed.get("current")
        state.previous_release = parsed.get("previous")
        state.trial_active = parsed.get("trial") is not None
        state.deps_hash = parsed.get("deps_hash")

    launcher_hash = conn.run(f"sha256sum {quote(root + '/bin/harmony-launch')} 2>/dev/null")
    if launcher_hash.ok and launcher_hash.stdout.strip():
        state.launcher_sha256 = launcher_hash.stdout.split()[0]

    unit = conn.run("systemctl cat harmony-hub 2>/dev/null")
    state.systemd_unit_content = unit.stdout if unit.ok and unit.stdout.strip() else None
    state.service_active = conn.run("systemctl is-active --quiet harmony-hub").ok
    state.service_enabled = conn.run("systemctl is-enabled --quiet harmony-hub").ok

    state.pigpiod_enabled = conn.run("systemctl is-enabled --quiet pigpiod").ok

    state.passwordless_sudo = conn.run("sudo -n true").ok

    return state


def _exists(conn: Runner, path: str) -> bool:
    return conn.run(f"test -e {quote(path)}").ok


def _is_dir(conn: Runner, path: str) -> bool:
    return conn.run(f"test -d {quote(path)}").ok
