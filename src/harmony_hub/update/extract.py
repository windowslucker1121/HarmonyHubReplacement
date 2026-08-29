"""Unpacks a bundle without trusting a single byte of it.

Signature verification (`auth.py`) proves the bundle came from a machine
holding the shared token; it says nothing about what's *inside* the tar. A
compromised or buggy dev machine, or a token that leaked, still shouldn't be
able to write outside the staging directory, replace a symlink with one
pointing at `/etc`, or wedge the device by inflating a 200GB member from a
15MB upload. This module is the layer that assumes the tar itself is
hostile even when the signature checks out.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

from .manifest import is_allowed

#: A code bundle is source text; nothing legitimate is anywhere near this.
#: Generous headroom over a real bundle (a few MB) while still making a
#: zip-bomb member cost nothing to reject.
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 20_000


class UnsafeBundle(ValueError):
    """The tar contains something extraction refuses to trust."""


def _check_member(member: tarfile.TarInfo) -> None:
    if member.islnk() or member.issym():
        raise UnsafeBundle(f"{member.name}: links are not allowed in a bundle")
    if not (member.isfile() or member.isdir()):
        raise UnsafeBundle(f"{member.name}: only regular files and directories are allowed")
    if member.size > MAX_MEMBER_BYTES:
        raise UnsafeBundle(f"{member.name}: {member.size} bytes exceeds the per-file limit")

    name = member.name
    if name.startswith("/") or name.startswith("\\"):
        raise UnsafeBundle(f"{name}: absolute paths are not allowed")
    # `Path.parts` normalises both slash styles and rejects `..` wherever it
    # sits in the path, not just as a literal prefix -- catches
    # `src/../../etc/passwd` as readily as `../etc/passwd`.
    if ".." in Path(name).parts or Path(name).is_absolute():
        raise UnsafeBundle(f"{name}: path escapes the bundle root")
    if member.isfile() and not is_allowed(name):
        raise UnsafeBundle(f"{name}: not on the bundle allowlist")


def safe_extract(tar_path: "Path | str", dest_dir: "Path | str") -> int:
    """Extracts `tar_path` into `dest_dir`, which must already exist and be empty.

    Every member is validated *before* anything is written, so a tar that
    fails partway through validation leaves `dest_dir` untouched rather than
    a partially-trusted tree. Returns the number of files written.
    """
    dest_dir = Path(dest_dir)
    if not dest_dir.is_dir():
        raise UnsafeBundle(f"{dest_dir} does not exist")
    if any(dest_dir.iterdir()):
        raise UnsafeBundle(f"{dest_dir} is not empty")

    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) > MAX_MEMBERS:
                raise UnsafeBundle(f"{len(members)} entries exceeds the {MAX_MEMBERS} member limit")

            total = 0
            files_only = []
            for member in members:
                _check_member(member)
                if member.isfile():
                    total += member.size
                    files_only.append(member)
            if total > MAX_TOTAL_BYTES:
                raise UnsafeBundle(f"{total} total bytes exceeds the {MAX_TOTAL_BYTES} bundle limit")

            # Extracted only after every member in the tar has passed
            # inspection above -- that manual pass is the real guarantee
            # here, since `filter="data"` (Python's own hardening) only
            # exists from 3.11.4 onward and this project's floor is bare
            # 3.11 (see `pyproject.toml`). Passed when available anyway, as
            # a second independent check.
            try:
                tar.extractall(dest_dir, members=members, filter="data")
            except TypeError:
                tar.extractall(dest_dir, members=members)
            return len(files_only)
    except (tarfile.TarError, OSError) as err:
        # Not a readable bundle at all -- corrupted in transit, truncated,
        # or never a tar to begin with. Treated the same as any other
        # member that failed inspection, rather than let a raw
        # `tarfile`/`gzip` exception surface past this module's contract.
        raise UnsafeBundle(f"{tar_path} is not a readable tar.gz: {err}") from err
