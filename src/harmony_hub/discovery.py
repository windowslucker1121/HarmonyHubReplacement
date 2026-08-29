"""Finding the remote's network address from the settings page.

Two ways to do it, both blocking, minute-plus, *transmitting-or-listening*
handshakes that need physical action at the remote -- which is what makes
this the one kind of operation here that cannot be a request/response: each
runs as a background job with its own state, polled by the UI and
cancellable.

`harmony_receiver.pairing.discover_network_address` ("hub") needs a real
Harmony Hub physically in pairing mode, and is quick once it starts.
`harmony_receiver.pairing.sniff_network_address` ("sniff") needs no Hub at
all -- it listens for the remote's own ordinary transmissions instead -- but
is slower and less certain, since it depends on catching real traffic
rather than a Hub answering on request.

Either way this needs the radio to itself. A hub already listening on the
radio is stopped for the duration and started again once the search ends --
see `HubRuntime.start_discovery` -- rather than this module refusing to run
alongside it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Literal, Optional

from pydantic import BaseModel

logger = logging.getLogger("HUB.discovery")

DiscoveryState = Literal["idle", "running", "done", "failed", "cancelled"]
DiscoveryMethod = Literal["hub", "sniff"]

#: Long enough for someone to walk to the Hub and press its pairing button,
#: short enough that a forgotten job does not hold the radio all afternoon.
DEFAULT_TIMEOUT = 60.0

#: The hub-less search needs much longer -- it is waiting for real traffic
#: to land mid-sweep rather than answering a direct request -- plus its own
#: separate budget afterward to confirm what it found.
DEFAULT_SNIFF_TIMEOUT = 120.0
DEFAULT_SNIFF_VERIFY_TIMEOUT = 20.0

_START_DETAIL: "dict[DiscoveryMethod, str]" = {
    "hub": "Put the Harmony Hub into pairing mode (press its pair/reset button).",
    "sniff": "No Hub needed -- press and release buttons on the remote repeatedly.",
}


class DiscoveryStatus(BaseModel):
    """Where the search has got to."""

    state: DiscoveryState = "idle"
    method: DiscoveryMethod = "hub"
    detail: str = ""
    address: Optional[str] = None
    channel: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class DiscoveryJob:
    """One search at a time, owned by the runtime."""

    def __init__(self, csn_pin: str, ce_pin: str) -> None:
        self.csn_pin = csn_pin
        self.ce_pin = ce_pin
        self.status = DiscoveryStatus()
        self._task: Optional[asyncio.Task] = None
        self._cancelled = False

    @property
    def running(self) -> bool:
        return self.status.state == "running"

    def start(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        method: DiscoveryMethod = "hub",
        verify_timeout: float = DEFAULT_SNIFF_VERIFY_TIMEOUT,
        on_finish: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> DiscoveryStatus:
        if self.running:
            return self.status

        self._cancelled = False
        self.status = DiscoveryStatus(
            state="running",
            method=method,
            detail=_START_DETAIL[method],
            started_at=datetime.now(),
        )
        self._task = asyncio.create_task(self._run(timeout, method, verify_timeout, on_finish))
        return self.status

    def cancel(self) -> DiscoveryStatus:
        """Asks the search to stop at its next iteration.

        Cooperative rather than a task cancellation: the work is on a worker
        thread holding the radio, and abandoning it there would leave the
        hardware claimed for the life of the process.
        """
        if self.running:
            self._cancelled = True
            self.status.detail = "Cancelling…"
        return self.status

    async def _run(
        self,
        timeout: float,
        method: DiscoveryMethod,
        verify_timeout: float,
        on_finish: Optional[Callable[[], Awaitable[None]]],
    ) -> None:
        try:
            try:
                address, channel = await asyncio.to_thread(self._search, timeout, method, verify_timeout)
            except Exception as err:
                self._finish("failed", str(err))
                return

            if address is None:
                self._finish("cancelled", "Cancelled.")
                return

            self.status.address = address
            self.status.channel = channel
            self._finish("done", f"Found {address} on channel {channel}.")
        finally:
            # In a `finally` around the whole thing, after status is
            # finalised, so a hub that was stopped to free the radio comes
            # back on every path -- found, failed, or cancelled -- not just
            # the happy one.
            if on_finish is not None:
                try:
                    await on_finish()
                except Exception:
                    logger.exception("Resuming the hub after address discovery failed")

    def _search(
        self, timeout: float, method: DiscoveryMethod, verify_timeout: float
    ) -> "tuple[Optional[str], Optional[int]]":
        """The blocking part, on a worker thread."""
        from harmony_receiver.pairing import PairingCancelled, discover_network_address, sniff_network_address
        from harmony_receiver.radio import create_radio, release_radio

        def _on_progress(message: str) -> None:
            # Called from this worker thread; the event loop thread only
            # ever reads `detail` for a status snapshot, and simple
            # attribute assignment is atomic enough under the GIL for a
            # progress string -- the same bar `_cancelled` below is held to.
            self.status.detail = message

        radio = create_radio(self.csn_pin, self.ce_pin)
        try:
            if method == "sniff":
                address, channel = sniff_network_address(
                    radio,
                    timeout_sec=timeout,
                    verify_timeout_sec=verify_timeout,
                    should_stop=lambda: self._cancelled,
                    on_progress=_on_progress,
                )
            else:
                address, channel = discover_network_address(
                    radio, timeout_sec=timeout, should_stop=lambda: self._cancelled
                )
            return address.hex().upper(), channel
        except PairingCancelled:
            return None, None
        finally:
            # Always, on every path: the next thing anyone does here is start
            # the hub with the address they just found, and that needs the
            # pins back.
            release_radio(radio)

    def _finish(self, state: DiscoveryState, detail: str) -> None:
        self.status.state = state
        self.status.detail = detail
        self.status.finished_at = datetime.now()
        logger.info("Address discovery %s: %s", state, detail)
