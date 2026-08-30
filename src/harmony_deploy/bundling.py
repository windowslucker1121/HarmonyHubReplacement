"""Building one ready-to-ship bundle: tests, the web build, the tar itself.

Shared by `push` (HTTP) and `setup` (SSH) -- both start from exactly the
same bundle, built the same way; only what happens to it afterward differs.
"""

from __future__ import annotations

import getpass
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from harmony_hub.update.bundle import build_bundle, make_build_id
from harmony_hub.update.manifest import Manifest

from .errors import DeployError


def git_info(repo_root: Path) -> Tuple[str, bool]:
    """Returns (short sha, dirty), or ("", False) outside a git checkout -- a missing git must not block a build."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"], cwd=repo_root, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=repo_root, text=True, stderr=subprocess.DEVNULL
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return "", False
    return sha, dirty


def run_tests(repo_root: Path) -> None:
    print("Running the test suite (pass --no-tests to skip)")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=repo_root)
    if result.returncode != 0:
        raise DeployError("tests failed -- fix them, or re-run with --no-tests if certain")


def build_web(repo_root: Path, build_id: str) -> Optional[Path]:
    # Resolved explicitly rather than handing the bare name "flutter" to
    # subprocess.run: on Windows the real executable is `flutter.bat`, and
    # CreateProcess (what subprocess uses when shell=False) does not search
    # PATHEXT the way typing the command into a shell does -- only
    # `shutil.which`, or a shell, will actually find it there.
    flutter = shutil.which("flutter")
    if flutter is None:
        raise DeployError("flutter is not on PATH -- install it, or re-run with --no-web to skip this step")

    print("Building the web UI (flutter build web --release)")
    # BUILD_ID is compiled into the app (see app/lib/main.dart) so a page
    # left open from before this deploy can tell its own build apart from
    # what the hub now reports at /api/version and offer to reload.
    result = subprocess.run(
        [flutter, "build", "web", "--release", f"--dart-define=BUILD_ID={build_id}"],
        cwd=repo_root / "app",
    )
    if result.returncode != 0:
        raise DeployError("flutter build web failed -- see output above, or re-run with --no-web to skip it")
    return repo_root / "app" / "build" / "web"


def created_by() -> str:
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except OSError:
        return ""


def build_release_bundle(
    repo_root: Path,
    *,
    run_tests_first: bool = True,
    build_web_first: bool = True,
    out_dir: Optional[Path] = None,
    write_manifest_json: bool = False,
) -> Tuple[Manifest, Path]:
    """Runs tests, builds the web UI, and packs both plus the Python source into one bundle.

    Returns the manifest and the tar.gz path -- everything a caller needs
    either to sign and POST it (`push`), to upload and unpack it directly
    (`setup`'s bootstrap case, over SFTP + `tar` instead of HTTP), or to
    publish as GitHub release assets (`harmony-deploy build`, used by CI --
    see `.github/workflows/release.yml`). `write_manifest_json` writes the
    manifest as its own file next to the tar, since a GitHub release asset
    has to be an actual file, not a value only ever passed in memory.
    """
    if run_tests_first:
        run_tests(repo_root)
    else:
        print("Skipping tests (--no-tests)")

    git_sha, git_dirty = git_info(repo_root)
    if git_dirty:
        print("WARNING: uncommitted changes present -- this build is tagged dirty")
    build_id = make_build_id(git_sha, git_dirty)

    web_dir = build_web(repo_root, build_id) if build_web_first else repo_root / "app" / "build" / "web"
    if not build_web_first:
        print("Skipping the web build (--no-web); reusing whatever is already at app/build/web")
    if web_dir is not None and not web_dir.is_dir():
        print("No built web UI found -- this bundle will ship code only, keeping the device's current UI")
        web_dir = None

    out_dir = Path(out_dir) if out_dir is not None else repo_root / ".harmony-deploy"
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path = out_dir / f"{build_id}.tar.gz"
    manifest = build_bundle(
        repo_root,
        tar_path,
        build_id=build_id,
        web_dir=web_dir,
        web_build_id=build_id if web_dir else None,
        git_sha=git_sha,
        git_dirty=git_dirty,
        created_by=created_by(),
    )
    print(f"Built {tar_path} ({manifest.byte_count} bytes, {manifest.file_count} files)")

    if write_manifest_json:
        manifest_path = out_dir / f"{build_id}.manifest.json"
        manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        print(f"Wrote {manifest_path}")

    return manifest, tar_path
