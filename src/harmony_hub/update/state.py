"""Which release is running, which one it can fall back to, and enough
history to answer "did the last deploy actually work" a week later.

Lives in `data/`, not the code tree, for the same reason `hub_settings.json`
does: `current`/`previous` here are what the *next boot* reads, so a deploy
that restarts into a bundle that cannot even import must not have already
destroyed the only interpreter that could still fix it.

`current`/`previous` are plain fields in this JSON file rather than OS
symlinks on purpose -- the same mechanism has to work identically on the Pi
and on a Windows dev box running the same code under test, and it keeps
"where do I boot from" answerable by reading one already-atomically-written
file instead of also trusting the filesystem's symlink state.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import ConfigDict, Field

from ..models import Base
from ..storage import write_json

logger = logging.getLogger("HUB.update.state")

DEFAULT_RELEASES_TO_KEEP = 3


class Trial(Base):
    """A release that has been activated but not yet proven to boot cleanly.

    Cleared by `confirm()` once the process has come up and served at least
    one request; until then, `launcher.py` counts boot attempts against
    `attempts` and falls back to `from_release` once they run out.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    release: str
    attempts: int = 0
    started_at: str
    from_release: Optional[str] = Field(default=None, alias="from")


class HistoryEntry(Base):
    build_id: str
    installed_at: str
    outcome: Literal["good", "rolled_back", "failed"]


class UpdateState(Base):
    current: Optional[str] = None
    previous: Optional[str] = None
    trial: Optional[Trial] = None
    history: List[HistoryEntry] = []
    last_nonce: int = 0

    #: Digest of the requirements last installed into the shared venv, so a
    #: redeploy with unchanged dependencies can skip `pip install` -- slow
    #: and pointless on a Pi when nothing in `pyproject.toml` moved.
    deps_hash: Optional[str] = None


def load(path: "Path | str") -> UpdateState:
    """Reads the update state, or starts fresh -- a missing or corrupt file is a first run, not an error."""
    path = Path(path)
    if not path.exists():
        return UpdateState()
    try:
        return UpdateState.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as err:
        logger.error("Could not read %s: %s -- starting from a fresh update state", path, err)
        return UpdateState()


def save(state: UpdateState, path: "Path | str") -> None:
    write_json(state.model_dump(mode="json", by_alias=True), path)


# ------------------------------------------------------------------
# Release directories
# ------------------------------------------------------------------


def release_dir(root: "Path | str", build_id: str) -> Path:
    return Path(root) / "releases" / build_id


def list_releases(root: "Path | str") -> List[str]:
    releases_dir = Path(root) / "releases"
    if not releases_dir.is_dir():
        return []
    return sorted(p.name for p in releases_dir.iterdir() if p.is_dir() and not p.name.endswith(".tmp"))


def current_src(root: "Path | str", state: UpdateState) -> Optional[Path]:
    """Where `PYTHONPATH` should point for the active release, or `None` before any release exists."""
    if state.current is None:
        return None
    return release_dir(root, state.current) / "src"


def prune(root: "Path | str", state: UpdateState, keep: int = DEFAULT_RELEASES_TO_KEEP) -> List[str]:
    """Removes old release directories, always keeping `current`, `previous`, and the newest `keep`.

    Logs but does not raise on a directory that won't delete: pruning is
    housekeeping after a deploy that has already succeeded, and must not be
    what turns a good deploy into a failed one.
    """
    keep_ids = {state.current, state.previous} - {None}
    releases = list_releases(root)
    for build_id in sorted(releases, reverse=True)[: max(keep, 0)]:
        keep_ids.add(build_id)

    removed = []
    for build_id in releases:
        if build_id in keep_ids:
            continue
        path = release_dir(root, build_id)
        try:
            shutil.rmtree(path)
            removed.append(build_id)
        except OSError:
            logger.exception("Could not remove old release %s", path)
    return removed


# ------------------------------------------------------------------
# State transitions -- pure functions, so the bookkeeping is testable
# without a filesystem in sight.
# ------------------------------------------------------------------


def activate(state: UpdateState, build_id: str) -> UpdateState:
    """Stages `build_id` as the current release, on trial until `confirm()`."""
    return state.model_copy(
        update={
            "previous": state.current,
            "current": build_id,
            "trial": Trial(release=build_id, attempts=0, started_at=_now(), from_release=state.current),
        }
    )


def confirm(state: UpdateState) -> UpdateState:
    """The trial release booted and served: record it as good and clear the trial."""
    if state.trial is None:
        return state
    entry = HistoryEntry(build_id=state.trial.release, installed_at=_now(), outcome="good")
    return state.model_copy(update={"trial": None, "history": [*state.history, entry][-50:]})


class NoPreviousRelease(RuntimeError):
    """There is nothing recorded to roll back to."""


def rollback(state: UpdateState) -> UpdateState:
    """Swaps `current` and `previous`. Rolling back twice returns to where you started."""
    if state.previous is None:
        raise NoPreviousRelease("no previous release recorded")
    history = state.history
    if state.current is not None:
        entry = HistoryEntry(build_id=state.current, installed_at=_now(), outcome="rolled_back")
        history = [*history, entry][-50:]
    return state.model_copy(
        update={"current": state.previous, "previous": state.current, "trial": None, "history": history}
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
