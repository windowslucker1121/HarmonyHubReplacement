"""Caches whether a newer GitHub release exists, and polls for one in the background.

`source.py` answers "what is the latest release" with a network round trip;
this module is what makes that cheap to ask from `/api/update/available`
(a plain read, like `/api/version`) and what keeps the answer fresh without
every browser tab polling GitHub itself. Its cache lives in
`data/update_check.json` -- in `data/`, not the code tree, for the same
reason `update_state.json` does: an update can never touch it, and it has
to survive one.

Failure here is expected, not exceptional: a Pi with no WAN, or GitHub
having a bad day, must show "nothing new" rather than an error banner --
see `check_now`'s handling of `source.ReleaseFeedError`. The one thing
this module keeps a person from missing is a release that showed up while
nobody was looking: `check_now` publishes an `update` event the first time
a given build becomes available, not on every poll that happens to still
see it -- see `announced_build_id`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import httpx

from ..events import EventBroker, HubEvent
from ..models import Base
from ..storage import write_json
from . import source
from . import state as state_module

if TYPE_CHECKING:
    from ..settings import HubSettings

logger = logging.getLogger("HUB.update.check")

#: How often the background poller wakes to see whether a check is due --
#: not how often it actually checks GitHub. Short relative to any sensible
#: `update_check_interval_hours` so a Settings change (including switching
#: checks off) takes effect within about a minute rather than up to a whole
#: interval later, and so the loop cancels promptly on shutdown.
POLL_WAKE_SECONDS = 60.0

#: Floor under `/api/update/check` (the user-initiated "check now" button):
#: GitHub's unauthenticated rate limit is 60 requests/hour per IP, shared
#: with whatever else on the LAN calls github.com, so a button that can be
#: mashed needs its own ceiling independent of `update_check_interval_hours`.
MIN_MANUAL_CHECK_SECONDS = 60.0


class CheckState(Base):
    last_checked_at: Optional[str] = None
    #: Set only when the most recent check failed; cleared on the next
    #: success. `available` deliberately is not cleared alongside it -- a
    #: transient failure must not make a real, previously-seen release
    #: disappear from the Settings screen.
    last_error: Optional[str] = None
    available: Optional[source.AvailableRelease] = None
    #: The build id `check_now` last published an `update` event for, so a
    #: release that is still the latest on the next ten polls does not say
    #: so ten times in the Live log.
    announced_build_id: Optional[str] = None


def state_path(update_root: "Path | str") -> Path:
    return Path(update_root) / "data" / "update_check.json"


def _update_state_path(update_root: "Path | str") -> Path:
    return Path(update_root) / "data" / "update_state.json"


def load(path: "Path | str") -> CheckState:
    """Reads the cached check, or starts fresh -- a missing or corrupt file is a first run, not an error."""
    path = Path(path)
    if not path.exists():
        return CheckState()
    try:
        return CheckState.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as err:
        logger.warning("Could not read %s: %s -- starting from a fresh update-check state", path, err)
        return CheckState()


def save(state: CheckState, path: "Path | str") -> None:
    write_json(state.model_dump(mode="json"), path)


def seconds_since(timestamp: Optional[str]) -> float:
    """How long ago an ISO timestamp was, or `+inf` for `None`/unparseable -- always "due" in that case."""
    if not timestamp:
        return float("inf")
    try:
        then = datetime.fromisoformat(timestamp)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def is_check_due(state: CheckState, min_interval_seconds: float) -> bool:
    return seconds_since(state.last_checked_at) >= min_interval_seconds


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def check_now(
    update_root: "Path | str",
    repo: str,
    *,
    broker: Optional[EventBroker] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> CheckState:
    """Asks GitHub for the latest release, updates the cache, and returns it.

    Always makes the network request -- callers that want the manual-check
    floor or the poll interval respected call `is_check_due` first (see
    `poll_forever` below, and the `/api/update/check` route in `api.py`).
    """
    update_root = Path(update_root)
    previous = load(state_path(update_root))
    current_build_id = state_module.load(_update_state_path(update_root)).current
    now = _now()

    try:
        release = await source.fetch_latest(repo, client=client)
    except source.ReleaseFeedError as err:
        # Expected on a device with no WAN, a repo not yet configured, or a
        # transient GitHub outage -- logged at info, and `available` is left
        # exactly as it was rather than wiped out by a check that failed.
        logger.info("GitHub release check for %s did not complete: %s", repo, err)
        next_state = previous.model_copy(update={"last_checked_at": now, "last_error": str(err)})
        save(next_state, state_path(update_root))
        return next_state

    available = release if source.is_newer(release.build_id, current_build_id) else None

    announced_build_id = previous.announced_build_id
    if available is not None and broker is not None and available.build_id != announced_build_id:
        broker.publish(
            HubEvent(
                type="update",
                ok=True,
                detail=f"Release {available.tag} ({available.build_id}) is available -- see Settings to install it",
            )
        )
        announced_build_id = available.build_id

    next_state = CheckState(
        last_checked_at=now,
        last_error=None,
        available=available,
        announced_build_id=announced_build_id,
    )
    save(next_state, state_path(update_root))
    return next_state


async def poll_forever(
    update_root: "Path | str",
    settings_provider: "Callable[[], HubSettings]",
    *,
    broker: Optional[EventBroker] = None,
    client: Optional[httpx.AsyncClient] = None,
    wake_seconds: float = POLL_WAKE_SECONDS,
) -> None:
    """Checks for a new release on a loop, sleeping `wake_seconds` between wake-ups.

    Meant to be fired-and-forgotten from the app's lifespan, the same way
    `confirm.schedule_confirmation` is. `settings_provider` is called fresh
    on every wake-up rather than captured once at startup: `github_repo`,
    `update_check_interval_hours` and `github_updates_enabled` can all
    change from Settings without a restart (see `HubRuntime.apply_settings`),
    and a poller that captured them once would keep checking a repo nobody
    configured anymore, or keep checking after being switched off.
    """
    update_root = Path(update_root)
    while True:
        settings = settings_provider()
        if settings.github_updates_enabled and settings.update_check_interval_hours > 0:
            state = load(state_path(update_root))
            if is_check_due(state, settings.update_check_interval_hours * 3600):
                try:
                    await check_now(update_root, settings.github_repo, broker=broker, client=client)
                except Exception:
                    # `check_now` already turns a `ReleaseFeedError` into cached
                    # state rather than raising; this is the backstop against
                    # anything else unanticipated, since one bad wake-up must
                    # not end the loop for the rest of the process's life.
                    logger.exception("Unexpected error checking for a GitHub release")
        await asyncio.sleep(wake_seconds)
