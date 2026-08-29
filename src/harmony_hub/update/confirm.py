"""Marks a trial release good, from inside the process that release itself is running.

`launcher.py` decides at *boot* time whether a release has failed too many
times to try again. This is the other half: only the running process can
say a release actually works, since "started" and "works" are different
claims -- a bundle can import cleanly, pass its own smoke test, and still
wedge or crash moments after the ASGI lifespan reports success.

Health here is deliberately just "the process came up and is serving", not
"the hub is running" -- a backend with no radio plugged in is a `failed`
`HubRuntime` state the settings page already explains, and must never look
like a bad deploy.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import state as state_module

logger = logging.getLogger("HUB.update.confirm")

#: Long enough that a bundle which crashes shortly after startup -- not on
#: import, but the first time some background task actually runs -- gets
#: caught before it's declared good; short enough that a real crash-loop
#: (systemd restarting every few seconds) burns through `MAX_TRIAL_ATTEMPTS`
#: and rolls back well before a person notices anything is wrong.
CONFIRM_DELAY_SECONDS = 60.0


def state_path(update_root: "Path | str") -> Path:
    return Path(update_root) / "data" / "update_state.json"


def confirm_trial(update_root: "Path | str") -> bool:
    """Marks the current release good if it was on trial. Returns whether it changed anything."""
    path = state_path(update_root)
    current = state_module.load(path)
    if current.trial is None:
        return False
    confirmed = state_module.confirm(current)
    state_module.save(confirmed, path)
    logger.info("Release %s confirmed after staying up %.0fs", current.trial.release, CONFIRM_DELAY_SECONDS)
    return True


async def schedule_confirmation(update_root: "Path | str", delay: float = CONFIRM_DELAY_SECONDS) -> None:
    """Waits out the trial period, then confirms. Meant to be fired-and-forgotten from the app's lifespan."""
    await asyncio.sleep(delay)
    try:
        confirm_trial(update_root)
    except Exception:
        logger.exception("Could not confirm the trial release")
