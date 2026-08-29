"""Turns a verified, staged bundle into the running hub.

No FastAPI import anywhere in this module -- the API route in `api.py` is a
thin translation of one HTTP request onto `install()`, so this half is
covered by tests that need nothing heavier than `tmp_path` and a fake venv.

Ordered so that nothing observable changes until the very last step:
staging, dependency install and the smoke test all happen against a release
directory nobody is using yet, and `current` only moves once every step
before it has already succeeded. A failure anywhere above activation leaves
the previously-running release doing exactly what it was doing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

from ..events import EventBroker, HubEvent
from . import state as state_module
from .bundle import hash_requirements
from .extract import UnsafeBundle, safe_extract
from .manifest import Manifest, UnsupportedManifest, check_schema

logger = logging.getLogger("HUB.update.installer")

#: Refuse to start staging a bundle unless there is at least this multiple
#: of its size free -- the tar, its extracted copy, and headroom for
#: whatever `pip install` downloads all have to fit at once.
MIN_FREE_SPACE_MULTIPLE = 3
SMOKE_TEST_TIMEOUT = 30
PIP_INSTALL_TIMEOUT = 600

#: A code-only bundle. Generous over a real one (a few MB, tens with a full
#: CanvasKit web build) while still making an oversized upload cheap to reject.
MAX_UPLOAD_BYTES = 128 * 1024 * 1024

EmitFn = Callable[[str], None]


class InstallError(RuntimeError):
    """The update was rejected or failed before `current` changed."""


def venv_python(root: "Path | str") -> Path:
    """The interpreter releases should run under -- the long-lived venv, not this process's own."""
    root = Path(root)
    for candidate in (root / "venv" / "bin" / "python", root / "venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return candidate
    return Path(sys.executable)  # no venv on this root (tests, or a bare dev checkout)


def check_disk_space(root: "Path | str", bundle_bytes: int) -> None:
    usage = shutil.disk_usage(root)
    needed = bundle_bytes * MIN_FREE_SPACE_MULTIPLE
    if usage.free < needed:
        raise InstallError(
            f"only {usage.free} bytes free at {root}, want at least {needed} to stage this bundle safely"
        )


async def _run(cmd: "list[str]", *, timeout: float, env: Optional[dict] = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise InstallError(f"`{' '.join(cmd)}` timed out after {timeout}s")
    text = raw.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        # Tail only: a runaway pip install's full log is not what belongs in
        # a hub event that also has to fit on a phone screen.
        raise InstallError(f"`{' '.join(cmd)}` failed ({proc.returncode}):\n{text[-4000:]}")
    return text


async def _install_dependencies(root: Path, requirements_path: Path, emit: EmitFn) -> None:
    python = str(venv_python(root))
    output = await _run([python, "-m", "pip", "install", "-r", str(requirements_path)], timeout=PIP_INSTALL_TIMEOUT)
    for line in output.strip().splitlines()[-10:]:
        emit(f"pip: {line}")
    frozen = await _run([python, "-m", "pip", "freeze"], timeout=60)
    (requirements_path.parent / "frozen.txt").write_text(frozen, encoding="utf-8")


async def _smoke_test(root: Path, release_dir: Path) -> None:
    """Proves the release at least imports, before it becomes `current`.

    Catches exactly the failure mode this feature exists to prevent: a
    typo or a forgotten import pushed at the end of a long session, which
    otherwise would not surface until the hub had already restarted onto
    it with nothing left running to explain why.
    """
    python = str(venv_python(root))
    src = str(release_dir / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = src
    await _run([python, "-m", "compileall", "-q", src], timeout=SMOKE_TEST_TIMEOUT, env=env)
    await _run(
        [python, "-c", "import harmony_hub.server, harmony_receiver.receiver"],
        timeout=SMOKE_TEST_TIMEOUT,
        env=env,
    )


async def install(
    root: "Path | str",
    tar_path: "Path | str",
    manifest: Manifest,
    *,
    broker: Optional[EventBroker] = None,
    keep_releases: int = state_module.DEFAULT_RELEASES_TO_KEEP,
) -> state_module.UpdateState:
    """Verifies, stages, dependency-installs, smoke-tests and activates one bundle.

    Raises `InstallError` for anything that goes wrong before activation --
    at that point nothing on disk that matters has changed, beyond a
    staging directory this function has already cleaned up itself.
    """
    root = Path(root)
    tar_path = Path(tar_path)

    def emit(detail: str, ok: bool = True) -> None:
        logger.info("update: %s", detail)
        if broker is not None:
            broker.publish(HubEvent(type="update", ok=ok, detail=detail))

    try:
        check_schema(manifest)
    except UnsupportedManifest as err:
        raise InstallError(str(err)) from err

    if manifest.build_id in state_module.list_releases(root):
        raise InstallError(f"build {manifest.build_id} is already installed")

    check_disk_space(root, manifest.byte_count)

    state_path = root / "data" / "update_state.json"
    current_state = state_module.load(state_path)

    emit(f"Staging {manifest.build_id} ({manifest.byte_count} bytes, {manifest.file_count} files)")
    staging = state_module.release_dir(root, manifest.build_id + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        safe_extract(tar_path, staging)
    except UnsafeBundle as err:
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError(f"bundle failed validation: {err}") from err

    (staging / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    requirements_path = staging / "requirements.txt"
    requirements_text = requirements_path.read_text(encoding="utf-8") if requirements_path.exists() else ""
    deps_hash = hash_requirements(requirements_text)

    final_dir = state_module.release_dir(root, manifest.build_id)
    try:
        staging.rename(final_dir)
    except OSError as err:
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError(f"could not finalise staged release: {err}") from err

    emit(f"Extracted {manifest.file_count} files to {final_dir}")

    try:
        if deps_hash != current_state.deps_hash:
            emit("Installing dependencies -- this can take a while on a Pi")
            await _install_dependencies(root, final_dir / "requirements.txt", emit)
            current_state = current_state.model_copy(update={"deps_hash": deps_hash})
        else:
            emit("Dependencies unchanged; skipping pip install")

        await _smoke_test(root, final_dir)
        emit("Smoke test passed")
    except InstallError:
        shutil.rmtree(final_dir, ignore_errors=True)
        raise

    new_state = state_module.activate(current_state, manifest.build_id)
    state_module.save(new_state, state_path)

    removed = state_module.prune(root, new_state, keep=keep_releases)
    if removed:
        emit(f"Pruned old release(s): {', '.join(removed)}")

    emit(f"Release {manifest.build_id} activated -- restarting to apply it")
    return new_state
