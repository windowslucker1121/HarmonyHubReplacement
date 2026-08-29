"""The one IR learn job in flight at a time, shared across every `IrBackend`.

There is exactly one receiver (`HubSettings.ir_rx_pin`), so there can only
ever be one capture happening -- this is what enforces that, the same way
`DiscoveryJob` is the one thing allowed to hold the radio for an address
search (`discovery.py`). Unlike `DiscoveryJob`, a learn job is not a single
capture: it takes two agreeing presses of the same button before it will
report `"captured"`, because a partial or noisy single capture is the most
common way IR learning goes wrong -- see `normalise.agree`.

Scoped by `device_id` rather than being anonymous: the *owning* device can
freely restart, cancel, or retry its own job (teaching several commands in a
row is the ordinary flow), while a *different* device asking to learn while
one is in flight is refused, with the owner named in the reason.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Literal, Optional

from . import normalise
from .gateway import IrBusy, IrGateway, IrHardwareError, IrTimeout

logger = logging.getLogger("HUB.ir.learn")

#: Long enough that someone can find the right button on an unfamiliar
#: remote and press it twice; short enough that an abandoned job does not
#: hold the receiver for the rest of the afternoon.
DEFAULT_TIMEOUT = 20.0


@dataclass(frozen=True)
class LearnStatus:
    """Where one IR learn job has got to.

    Lives here rather than beside `Pairable` in `backends/__init__.py`: it
    is IR's own state -- what the shared receiver and the shared job are
    doing -- not a property of the `Backend`/`Learnable` interface itself,
    which only ever passes it through. `backends/__init__.py` references
    this under `TYPE_CHECKING` for `Learnable`'s signatures rather than
    importing it for real, which is what keeps that package from depending
    on this one the way this one -- deliberately -- does not depend on it.

    `"idle"` before anything starts; `"waiting"` for the first capture;
    `"confirming"` once it has one and wants a second press to check against;
    `"captured"` once two presses agreed -- `decoded` and `pulses` are only
    meaningful from here on; `"mismatch"` when they did not, which is not
    fatal -- see `Learnable`'s docstring -- and `"failed"` for anything that
    stopped the job outright (the receiver went away, the timeout elapsed).
    """

    state: Literal["idle", "waiting", "confirming", "captured", "mismatch", "failed"]
    detail: str = ""
    #: A best-effort protocol label ("NEC 0x04 0x08"), or "" if none matched
    #: -- purely cosmetic, see `normalise.decode`.
    decoded: str = ""
    pulses: int = 0


class IrLearnJob:
    """State machine for one learn attempt, from the first press to `finish`."""

    def __init__(self) -> None:
        self._device_id: Optional[str] = None
        self._status = LearnStatus(state="idle")
        self._task: Optional[asyncio.Task] = None
        self._confirmed: Optional[List[int]] = None
        self._decoded = ""

    @property
    def owner(self) -> Optional[str]:
        """Which device currently holds the receiver, or `None` if idle."""
        return self._device_id

    @property
    def result(self) -> Optional[List[int]]:
        """The captured-and-confirmed timings, once the job has reached `"captured"`."""
        return self._confirmed

    @property
    def decoded(self) -> str:
        return self._decoded

    def status(self, device_id: str) -> LearnStatus:
        """`device_id`'s view of the job -- idle if it does not own it."""
        if device_id != self._device_id:
            return LearnStatus(state="idle")
        return self._status

    def start(self, device_id: str, gateway: IrGateway, timeout: float = DEFAULT_TIMEOUT) -> LearnStatus:
        """Begins a capture for `device_id`. Synchronous and atomic -- see below.

        Refuses with `IrBusy` when a *different* device already owns the
        receiver *and* is actually using it -- `"waiting"`, `"confirming"`,
        or holding an unsaved `"captured"` result someone could still come
        back to save. A job that ended in `"failed"` or `"mismatch"` is not
        protected: there is nothing left to lose by letting a different
        device (or the same one) start over, and requiring an explicit
        `learn_cancel()` first would leave the receiver stuck unusable by
        anyone after every ordinary failure -- exactly the trap a Pi with
        `pigpiod` not yet started fell into before `IrGateway._ensure_connected`
        existed: the first learn attempt would fail, and it alone would then
        own a receiver nobody could use again without a hub restart.

        The owning device restarting its own job (a fresh attempt after a
        mismatch, or moving on to the next command) is not a conflict either
        and always succeeds, cancelling whatever this job was doing before.

        No `await` happens between the ownership check and claiming it, so
        two overlapping calls from different devices cannot both pass the
        check before either has set `_device_id` -- asyncio only ever runs
        one task at a time between `await` points, and there is not one here.
        """
        held = self._status.state not in ("idle", "failed", "mismatch")
        if self._device_id is not None and self._device_id != device_id and held:
            raise IrBusy(f"the IR receiver is already learning for '{self._device_id}'")

        if self._task is not None:
            self._task.cancel()

        self._device_id = device_id
        self._confirmed = None
        self._decoded = ""
        self._status = LearnStatus(
            state="waiting", detail="Point the remote at the receiver and press the button."
        )
        self._task = asyncio.create_task(self._run(gateway, timeout))
        return self._status

    def cancel(self, device_id: str) -> LearnStatus:
        if device_id != self._device_id:
            return LearnStatus(state="idle")
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._reset()
        return self._status

    def finish(self, device_id: str) -> None:
        """Frees the receiver once a capture has been saved or discarded."""
        if device_id == self._device_id:
            self._reset()

    def _reset(self) -> None:
        self._device_id = None
        self._confirmed = None
        self._decoded = ""
        self._status = LearnStatus(state="idle")

    async def _run(self, gateway: IrGateway, timeout: float) -> None:
        try:
            first = await gateway.capture(timeout)
            self._status = LearnStatus(
                state="confirming",
                detail="Got it. Press the same button once more to confirm.",
                pulses=len(first),
            )
            second = await gateway.capture(timeout)
        except asyncio.CancelledError:
            raise
        except (IrTimeout, IrHardwareError) as err:
            self._status = LearnStatus(state="failed", detail=str(err))
            return
        except Exception as err:  # pragma: no cover - unexpected hardware failure
            logger.exception("IR learn job failed unexpectedly")
            self._status = LearnStatus(state="failed", detail=str(err))
            return

        if not normalise.agree(first, second):
            self._status = LearnStatus(
                state="mismatch",
                detail="Those two presses didn't match. Try again, holding the remote steadier.",
            )
            return

        confirmed = normalise.normalise(first)
        decoded = normalise.decode(confirmed)
        self._confirmed = confirmed
        self._decoded = decoded
        self._status = LearnStatus(
            state="captured", detail=decoded or "Captured.", decoded=decoded, pulses=len(confirmed)
        )


_JOB = IrLearnJob()


def job() -> IrLearnJob:
    """The one learn job for this process."""
    return _JOB
