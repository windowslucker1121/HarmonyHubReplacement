"""What a bundle is, independent of how it travels.

Both ends of a deploy need to agree on this without trusting each other: the
dev machine that builds a bundle and the device that unpacks one. Kept as
its own module -- no FastAPI, no filesystem writes -- so both `bundle.py`
(which only ever runs on a dev machine) and the on-device installer can
import it without dragging the other's concerns along.

The allowlist below is the whole answer to "config must not be transferred".
It is deliberately a *positive* list of what goes in, not a list of what to
exclude -- a new gitignored file (a future `secrets.json`, say) is excluded
by default under this scheme and would have to be added on purpose, rather
than silently swept into a bundle because nobody remembered to blocklist it.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

#: The manifest format itself. Bumped only when a field's *meaning* changes
#: in a way an old installer would misread -- not for every new field, which
#: pydantic already tolerates via `extra="ignore"` on the receiving end (see
#: `Manifest.model_config` override below).
SCHEMA_VERSION = 1

#: Relative-path globs that belong in a bundle. Matched with `fnmatch`
#: against POSIX-style relative paths, so `**` is not recursive the way
#: `pathlib.Path.glob` treats it -- `_iter_allowed` below walks the tree
#: itself and tests each file against every pattern instead of relying on
#: glob recursion semantics.
ALLOWED_PATTERNS: List[str] = [
    "requirements.txt",
    "src/harmony_hub/*.py",
    "src/harmony_hub/backends/*.py",
    "src/harmony_hub/update/*.py",
    "src/harmony_receiver/*.py",
    "src/harmony_hub/web/*",
    "src/harmony_hub/web/*/*",
    "src/harmony_hub/web/*/*/*",
    "src/harmony_hub/web/*/*/*/*",
    "src/harmony_hub/web/*/*/*/*/*",
]

#: Never matched even if a future pattern above would otherwise catch them.
#: Belt and braces over the allowlist: a `hub_config.json` someone commits
#: inside `src/` by mistake must still never ship.
DENY_NAMES = {
    "hub_settings.json",
    "hub_config.json",
    "buttons.json",
    "codes",
    "__pycache__",
}


def is_allowed(relative_path: "Path | str") -> bool:
    """Whether `relative_path` (POSIX-style, relative to the repo root) belongs in a bundle."""
    posix = Path(relative_path).as_posix()
    if any(part in DENY_NAMES for part in Path(posix).parts):
        return False
    if Path(posix).suffix == ".pyc":
        return False
    return any(fnmatch.fnmatch(posix, pattern) for pattern in ALLOWED_PATTERNS)


def iter_allowed_files(root: "Path | str"):
    """Yields every file under `root` that the allowlist admits, as paths relative to `root`."""
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if is_allowed(relative):
            yield relative


class Manifest(BaseModel):
    """Describes one bundle: what it is, and enough to prove it wasn't tampered with in transit.

    `content_sha256` is checked before the tar is ever opened -- see
    `extract.py` -- so a corrupted or tampered upload is rejected without a
    single byte of it being trusted enough to decompress.

    Unlike the hub's own config models this tolerates unknown fields
    (`extra="ignore"`) rather than forbidding them: a newer build may add an
    informational field, and an older installer reading it should not choke
    on that field before it even gets to `check_schema`, which is where a
    genuinely incompatible bundle is meant to be refused, with a message
    that says why instead of a generic validation error.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    build_id: str = Field(min_length=1)
    git_sha: str = ""
    git_dirty: bool = False
    created_at: str  # ISO 8601; kept a plain string so an old installer that
    # only knows an older schema can still read it back for display.
    created_by: str = ""
    python_requires: str = ">=3.11"
    deps_hash: str = ""
    web_build_id: Optional[str] = None
    file_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    content_sha256: str = Field(min_length=64, max_length=64)


class UnsupportedManifest(ValueError):
    """The bundle declares a schema this installer predates."""


def check_schema(manifest: Manifest) -> None:
    if manifest.schema_version > SCHEMA_VERSION:
        raise UnsupportedManifest(
            f"bundle uses manifest schema {manifest.schema_version}, this hub only understands "
            f"up to {SCHEMA_VERSION} -- update the hub itself first"
        )
