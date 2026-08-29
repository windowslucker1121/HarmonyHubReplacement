"""harmony-deploy setup: provision or update one device over SSH.

Where push assumes a hub is already running and healthy enough to accept
a signed HTTP request, this assumes nothing -- a bare Pi with nothing on it
yet is exactly what this exists for. One command handles both a first
install and a routine update: probe.inspect reads what is actually on the
device, plan.build_plan decides what is missing, and provision.py does
only that. Run again against a device already fully up to date and the
plan is just confirm-and-refresh-the-token -- so this is safe to run more
than once, and safe to run for the very first time.

Does not touch the HTTP path at all -- push keeps working exactly as it
did before this module existed. SSH is a second way in for when HTTP
cannot be used yet: nothing running to push to, the token never fetched,
the unit missing, the process crash-looping.
"""

from __future__ import annotations

import getpass as getpass_module
from pathlib import Path
from typing import Callable, Optional

import harmony_hub

from . import provision
from .bundling import build_release_bundle
from .errors import DeployError
from .plan import PlanError, Step, StepKind, build_plan, local_launcher_sha256
from .probe import CannotResolveRoot, DeviceState, inspect, resolve_remote_path
from .ssh import AuthenticationFailed, Connection, SshError
from .targets import save_target
from .verify import wait_for_version

LAUNCHER_PATH = Path(harmony_hub.__file__).resolve().parent / "update" / "launcher.py"

TrustPrompt = Callable[[str, str], bool]


def default_trust_prompt(hostname: str, fingerprint: str) -> bool:
    answer = input(f"  Host key {fingerprint} for {hostname} -- trust it? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def connect(
    host: str,
    user: str,
    *,
    port: int = 22,
    password: Optional[str] = None,
    trust_host: TrustPrompt = default_trust_prompt,
) -> Connection:
    """Tries key/agent auth first; only prompts for a password if that is rejected.

    A password given up front skips straight to using it -- this is what
    lets a scripted invocation avoid a terminal prompt entirely, while an
    interactive run gets the "keys just work" experience once set up.
    """
    if password is not None:
        return Connection.open(host, user, password=password, port=port, trust_host=trust_host)
    try:
        return Connection.open(host, user, password=None, port=port, trust_host=trust_host)
    except AuthenticationFailed:
        typed = getpass_module.getpass(f"Password for {user}@{host}: ")
        return Connection.open(host, user, password=typed, port=port, trust_host=trust_host)


def run_inspect(host: str, user: str, root: str, *, port: int = 22, password: Optional[str] = None) -> DeviceState:
    """Read-only: reports what is on the device without changing anything."""
    try:
        with connect(host, user, port=port, password=password) as conn:
            return inspect(conn, resolve_remote_path(conn, root))
    except (SshError, CannotResolveRoot) as err:
        raise DeployError(str(err)) from err


def print_state(state: DeviceState) -> None:
    print(
        f"  {state.arch or 'unknown arch'}, {state.python_version or 'python3 not found'}, "
        f"{state.free_bytes // (1024 * 1024)} MB free at {state.root}"
    )
    print(
        f"  layout: venv={'yes' if state.venv_exists else 'no'} "
        f"releases/={'yes' if state.releases_dir_exists else 'no'} "
        f"data/={'yes' if state.data_dir_exists else 'no'} "
        f"bin/={'yes' if state.bin_dir_exists else 'no'}"
    )
    if state.releases:
        print(f"  releases on disk: {', '.join(state.releases)}")
    trial_note = " (on trial)" if state.trial_active else ""
    print(f"  current release: {state.current_release or '(none)'}{trial_note}")
    if state.config_at_old_location and not state.config_at_new_location:
        print("  config: still at the pre-migration location")
    elif state.config_at_old_location and state.config_at_new_location:
        print("  config: present at BOTH locations -- ambiguous")
    if state.stale_editable_install:
        print("  stale editable install detected (venv/bin/harmony-hub)")
    unit_note = "installed" if state.systemd_unit_content else "not installed"
    active_note = "active" if state.service_active else "not active"
    print(f"  systemd unit: {unit_note}, service {active_note}")
    print(f"  pigpiod: {'enabled' if state.pigpiod_enabled else 'not installed/enabled'}")
    print(f"  sudo: {'passwordless' if state.passwordless_sudo else 'needs a password'}")


def print_plan(steps) -> None:
    print("Plan:")
    for i, step in enumerate(steps, 1):
        note = "  [needs sudo]" if step.needs_sudo else ""
        print(f"  {i}. {step.description}{note}")


def run_setup(
    repo_root: Path,
    target_name: str,
    *,
    host: str,
    user: str,
    root: str,
    port: int = 22,
    password: Optional[str] = None,
    run_tests_first: bool = True,
    build_web_first: bool = True,
    dry_run: bool = False,
    assume_yes: bool = False,
    trust_host: TrustPrompt = default_trust_prompt,
    targets_file: Optional[Path] = None,
    token_dir: Optional[Path] = None,
) -> None:
    manifest, tar_path = build_release_bundle(
        repo_root, run_tests_first=run_tests_first, build_web_first=build_web_first
    )

    print(f"Connecting to {user}@{host} ...")
    try:
        conn = connect(host, user, port=port, password=password, trust_host=trust_host)
    except SshError as err:
        raise DeployError(str(err)) from err

    try:
        print("Inspecting the device...")
        # Resolved before anything else touches it: `~` survives neither
        # shell quoting, nor SFTP, nor a systemd unit file. From here down,
        # `root` is absolute.
        try:
            root = resolve_remote_path(conn, root)
        except CannotResolveRoot as err:
            raise DeployError(str(err)) from err
        state = inspect(conn, root)
        print_state(state)

        try:
            steps = build_plan(
                state,
                build_id=manifest.build_id,
                user=user,
                deps_hash=manifest.deps_hash,
                launcher_sha256=local_launcher_sha256(LAUNCHER_PATH),
            )
        except PlanError as err:
            raise DeployError(str(err)) from err

        print_plan(steps)

        if dry_run:
            print("--dry-run: not changing anything")
            return

        if not assume_yes:
            answer = input("Proceed? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return

        _execute(conn, steps, state, root=root, user=user, manifest=manifest, tar_path=tar_path)

        url = f"http://{host}:8765"
        print(f"Waiting for {url} to come back on {manifest.build_id} ...")
        wait_for_version(url, manifest.build_id)

        print("Fetching the update token...")
        token = provision.fetch_token(conn, root)
    finally:
        conn.close()

    token_dir = token_dir or (Path.home() / ".harmony")
    token_dir.mkdir(parents=True, exist_ok=True)
    token_path = token_dir / f"{target_name}.token"
    token_path.write_bytes(token)
    try:
        token_path.chmod(0o600)
    except OSError:
        pass

    save_kwargs = {"path": targets_file} if targets_file is not None else {}
    save_target(target_name, {"url": url, "token_file": str(token_path)}, **save_kwargs)
    print(f"Wrote deploy_targets.json entry {target_name!r} -> {url}")
    print(f"harmony-deploy push {target_name}   # for every ordinary update from here on")


def _execute(conn, steps, state: DeviceState, *, root: str, user: str, manifest, tar_path: Path) -> None:
    def emit(message: str) -> None:
        print(f"     {message}")

    for step in steps:
        if step.kind in (StepKind.VERIFY, StepKind.FETCH_TOKEN):
            continue  # handled by the caller, once the device is reachable over HTTP again

        print(f"-> {step.description}")
        if step.kind is StepKind.CREATE_LAYOUT:
            provision.create_layout(conn, root)
        elif step.kind is StepKind.CREATE_VENV:
            provision.create_venv(conn, root)
        elif step.kind is StepKind.INSTALL_PIGPIO:
            provision.install_pigpio(conn, emit)
        elif step.kind is StepKind.MIGRATE_CONFIG:
            provision.migrate_config(conn, root)
        elif step.kind is StepKind.REMOVE_STALE_INSTALL:
            provision.remove_stale_install(conn, root)
        elif step.kind is StepKind.UPLOAD_LAUNCHER:
            provision.upload_launcher(conn, root, LAUNCHER_PATH)
        elif step.kind is StepKind.BOOTSTRAP_RELEASE:
            provision.bootstrap_release(conn, root, tar_path, manifest.build_id, emit)
        elif step.kind is StepKind.UPDATE_RELEASE:
            assert state.current_release is not None
            provision.update_release(conn, root, tar_path, manifest, current_build=state.current_release, emit=emit)
        elif step.kind is StepKind.WRITE_SYSTEMD_UNIT:
            provision.write_systemd_unit(conn, root, user)
        elif step.kind is StepKind.RESTART_SERVICE:
            provision.restart_service(conn)
