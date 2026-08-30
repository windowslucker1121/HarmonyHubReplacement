"""Builds a code-only bundle. Runs on the dev machine, never on the device.

Produces two things, kept deliberately separate rather than one file with
the other embedded inside it: a `Manifest` (small, JSON, easy to log and to
sign) and a tar.gz of the allowlisted source tree. Keeping them apart avoids
a chicken-and-egg hash: `Manifest.content_sha256` covers the tar bytes
exactly as they will travel over HTTP, which would be circular if the
manifest carrying that hash were itself inside the tar being hashed.
"""

from __future__ import annotations

import hashlib
import tarfile
import tomllib
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

from .manifest import WEB_PREFIX, Manifest, iter_allowed_files


def read_requirements(pyproject_path: "Path | str") -> str:
    """The runtime dependency list from `[project.dependencies]`, one per line.

    Read from `pyproject.toml` rather than `pip freeze` on the dev machine:
    freezing would capture dev-only packages (pytest, etc.) and pin exact
    versions the device has no reason to match. What the device installs
    into its own long-lived venv is `>=` ranges, same as any other install.
    """
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    deps = data["project"]["dependencies"]
    return "\n".join(sorted(deps)) + "\n"


def hash_requirements(requirements_text: str) -> str:
    """A stable digest of a requirements list, so re-deploying unchanged deps skips `pip install`."""
    normalised = "\n".join(sorted(line.strip() for line in requirements_text.splitlines() if line.strip()))
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _collect_members(repo_root: Path, web_dir: Optional[Path]) -> "list[tuple[str, Path]]":
    """`(arcname, source_path)` pairs, sorted, for everything the bundle will contain.

    The web build is spliced in under `src/harmony_hub/web/` -- where
    `find_ui_dir` looks first (see `server.py`) -- even though nothing lives
    there in the working tree; on a dev checkout the built UI is at
    `app/build/web`, gitignored, rebuilt on demand.
    """
    members: "list[tuple[str, Path]]" = []
    for relative in iter_allowed_files(repo_root):
        arcname = relative.as_posix()
        if arcname.startswith(WEB_PREFIX):
            continue  # never present in a working tree; spliced in below instead
        members.append((arcname, repo_root / relative))

    if web_dir is not None and Path(web_dir).is_dir():
        for path in sorted(Path(web_dir).rglob("*")):
            if path.is_file():
                arcname = WEB_PREFIX + path.relative_to(web_dir).as_posix()
                members.append((arcname, path))

    members.sort(key=lambda pair: pair[0])
    return members


def build_bundle(
    repo_root: "Path | str",
    output_path: "Path | str",
    *,
    build_id: str,
    web_dir: "Path | str | None" = None,
    web_build_id: Optional[str] = None,
    git_sha: str = "",
    git_dirty: bool = False,
    created_by: str = "",
) -> Manifest:
    """Writes the tar.gz to `output_path` and returns its manifest.

    Raises if the resulting bundle would be empty (an allowlist that matched
    nothing is a bug in the allowlist, not an empty-but-valid release) or if
    `web_dir` was given but does not exist (a deploy that silently drops the
    UI is worse than one that fails loudly).
    """
    repo_root = Path(repo_root)
    output_path = Path(output_path)
    if web_dir is not None:
        web_dir = Path(web_dir)
        if not web_dir.is_dir():
            raise FileNotFoundError(f"web_dir {web_dir} does not exist -- run `flutter build web` first")

    members = _collect_members(repo_root, web_dir)
    if not members:
        raise RuntimeError("bundle would be empty -- check the allowlist in update/manifest.py")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # `requirements.txt` is synthesised from `pyproject.toml` rather than
    # collected from the working tree -- the repo has no such file at its
    # root, and the device needs one either way to know what to
    # `pip install`. Written into the tar directly so it travels as an
    # ordinary bundle member the installer can just read back out.
    requirements_text = read_requirements(repo_root / "pyproject.toml")
    requirements_bytes = requirements_text.encode("utf-8")

    byte_count = 0
    with tarfile.open(output_path, "w:gz") as tar:
        info = tarfile.TarInfo(name="requirements.txt")
        info.size = len(requirements_bytes)
        tar.addfile(info, BytesIO(requirements_bytes))
        byte_count += len(requirements_bytes)

        for arcname, source in members:
            tar.add(source, arcname=arcname, recursive=False)
            byte_count += source.stat().st_size

    content_sha256 = hashlib.sha256()
    with output_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            content_sha256.update(chunk)

    return Manifest(
        build_id=build_id,
        git_sha=git_sha,
        git_dirty=git_dirty,
        created_at=datetime.now(timezone.utc).isoformat(),
        created_by=created_by,
        deps_hash=hash_requirements(requirements_text),
        web_build_id=web_build_id,
        file_count=len(members) + 1,
        byte_count=byte_count,
        content_sha256=content_sha256.hexdigest(),
    )


def make_build_id(git_sha: str = "", git_dirty: bool = False) -> str:
    """`<UTC timestamp>-<short sha>`, sortable and unique enough for a filename and a symlink target."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = git_sha[:7] if git_sha else "nogit"
    if git_dirty:
        suffix += "-dirty"
    return f"{stamp}-{suffix}"
