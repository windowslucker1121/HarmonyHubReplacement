"""Turns a device's current state plus a build you want on it into an ordered list of steps.

Pure -- no I/O, no SSH, no filesystem beyond hashing a path handed to it --
so every branch is covered by a table of `DeviceState` in, steps out, with
no network or Pi in sight. `provision.py` is what actually executes what
this decides; keeping the decision separate from the execution is the same
split `installer.install` (no FastAPI import) already uses on the device
side, for the same reason.

Ordering matters and is deliberate: layout before anything writes into it,
pigpiod installed early since it is the step most likely to fail (network,
an apt lock) and independent of everything after it, config migrated
before the old location could be mistaken for current, the stale install
removed before a fresh one could be shadowed by it, dependencies before the
release that needs them, the unit and restart last so nothing user-visible
changes until everything before it has succeeded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

from .probe import DeviceState


class PlanError(RuntimeError):
    """The device is in a state this tool refuses to guess its way through."""


class AmbiguousConfigLocation(PlanError):
    """Config exists at both the old and new location -- only a person can say which is current."""


class StepKind(str, Enum):
    CREATE_LAYOUT = "create_layout"
    CREATE_VENV = "create_venv"
    INSTALL_PIGPIO = "install_pigpio"
    MIGRATE_CONFIG = "migrate_config"
    REMOVE_STALE_INSTALL = "remove_stale_install"
    UPLOAD_LAUNCHER = "upload_launcher"
    BOOTSTRAP_RELEASE = "bootstrap_release"
    UPDATE_RELEASE = "update_release"
    WRITE_SYSTEMD_UNIT = "write_systemd_unit"
    RESTART_SERVICE = "restart_service"
    VERIFY = "verify"
    FETCH_TOKEN = "fetch_token"


@dataclass(frozen=True)
class Step:
    kind: StepKind
    description: str
    needs_sudo: bool = False


SYSTEMD_UNIT_TEMPLATE = """[Unit]
Description=Harmony Hub
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={root}/data
ExecStart={root}/venv/bin/python {root}/bin/harmony-launch {root}
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=10

[Install]
WantedBy=multi-user.target
"""

#: Restarting or (re)activating one of these changes what the running
#: process does, so a restart is needed regardless of whether the unit
#: file itself also changed.
_RELEASE_STEPS = (StepKind.BOOTSTRAP_RELEASE, StepKind.UPDATE_RELEASE)


def render_systemd_unit(root: str, user: str) -> str:
    return SYSTEMD_UNIT_TEMPLATE.format(root=root, user=user)


def unit_matches(existing: Optional[str], root: str, user: str) -> bool:
    """Whether the directives that matter are already right -- not a byte-for-byte match.

    `systemctl cat` prepends its own header comment and may reformat
    whitespace, so comparing whole file contents would report "different"
    on every device that is in fact already correct.
    """
    if not existing:
        return False
    needed = {
        f"User={user}",
        f"WorkingDirectory={root}/data",
        f"ExecStart={root}/venv/bin/python {root}/bin/harmony-launch {root}",
        "Restart=always",
    }
    lines = {line.strip() for line in existing.splitlines()}
    return needed.issubset(lines)


def local_launcher_sha256(launcher_path: "Path | str") -> str:
    return hashlib.sha256(Path(launcher_path).read_bytes()).hexdigest()


def build_plan(
    state: DeviceState,
    *,
    build_id: str,
    user: str,
    deps_hash: str,
    launcher_sha256: str,
    force_unit_rewrite: bool = False,
) -> List[Step]:
    """Returns the steps needed to bring `state`'s device up to `build_id`.

    Idempotent by construction: run this against a device already fully on
    `build_id` and the only steps back are `VERIFY` and `FETCH_TOKEN` --
    both read-only from the device's point of view, and cheap enough to
    always be worth confirming rather than trusted from a previous run.
    """
    if state.config_at_old_location and state.config_at_new_location:
        raise AmbiguousConfigLocation(
            f"hub_settings.json exists at both {state.root} and {state.root}/data -- "
            "resolve which one is current by hand before continuing"
        )

    steps: List[Step] = []

    if not (state.releases_dir_exists and state.data_dir_exists and state.bin_dir_exists):
        steps.append(Step(StepKind.CREATE_LAYOUT, "Create releases/ incoming/ data/ bin/"))

    if not state.venv_exists:
        steps.append(Step(StepKind.CREATE_VENV, "Create the venv (--system-site-packages)"))

    if not state.pigpiod_enabled:
        # Independent of everything else here and involves `apt-get`, the
        # slowest and most likely to fail step in the whole plan (no
        # internet, a held apt lock) -- run early so that fails before time
        # is spent uploading a release bundle, not after.
        steps.append(
            Step(
                StepKind.INSTALL_PIGPIO,
                "Install and enable pigpiod (for the IR backend)",
                needs_sudo=True,
            )
        )

    if state.config_at_old_location and not state.config_at_new_location:
        steps.append(
            Step(
                StepKind.MIGRATE_CONFIG,
                "Move hub_settings.json, hub_config.json, buttons.json, credentials/ into data/",
            )
        )

    if state.stale_editable_install:
        steps.append(
            Step(StepKind.REMOVE_STALE_INSTALL, "Remove the stale editable install (pip uninstall harmony-receiver)")
        )

    if state.launcher_sha256 != launcher_sha256:
        steps.append(Step(StepKind.UPLOAD_LAUNCHER, "Upload bin/harmony-launch"))

    if build_id not in state.releases:
        if state.current_release is None:
            steps.append(
                Step(StepKind.BOOTSTRAP_RELEASE, f"Upload and activate release {build_id} (nothing running yet)")
            )
        else:
            steps.append(
                Step(
                    StepKind.UPDATE_RELEASE,
                    f"Upload release {build_id} and install it through the current release's own installer",
                )
            )
    # else: already uploaded (and, in every case this tool creates, already
    # active) -- nothing to do for the release itself.

    if force_unit_rewrite or not unit_matches(state.systemd_unit_content, state.root, user):
        steps.append(Step(StepKind.WRITE_SYSTEMD_UNIT, "Write /etc/systemd/system/harmony-hub.service", needs_sudo=True))

    needs_restart = (
        any(step.kind in _RELEASE_STEPS or step.kind is StepKind.WRITE_SYSTEMD_UNIT for step in steps)
        or not state.service_active
    )
    if needs_restart:
        steps.append(Step(StepKind.RESTART_SERVICE, "Restart harmony-hub", needs_sudo=True))

    steps.append(Step(StepKind.VERIFY, "Wait for /api/version to report the new build"))
    steps.append(Step(StepKind.FETCH_TOKEN, "Read the update token and write deploy_targets.json"))

    return steps
