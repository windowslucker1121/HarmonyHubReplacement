"""Where button events come from.

Three sources, all producing the same `RemoteEvent` stream so the engine
cannot tell them apart:

* `RadioSource` -- the real nRF24 receiver.
* `ReplaySource` -- a recorded capture, replayed at its original timing.
* `ManualSource` -- presses injected over the API.

The last two are what make the platform developable. Building a scene
editor should not require holding a remote and pressing buttons on cue, and
the engine's tests should not require a radio at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

from harmony_receiver.events import RemoteEvent
from harmony_receiver.profiles import ButtonMap
from harmony_receiver.protocol import parse_frame
from harmony_receiver.tracking import PressTracker

if TYPE_CHECKING:  # pragma: no cover
    from .settings import HubSettings

logger = logging.getLogger("HUB.sources")


class EventSource:
    """Base class: an async iterator of remote events that can be shut down."""

    async def events(self) -> AsyncIterator[RemoteEvent]:
        raise NotImplementedError

    async def close(self) -> None:
        """Stop producing. Safe to call more than once."""


class ManualSource(EventSource):
    """Events pushed in by code -- the API's "simulate a press" and tests."""

    def __init__(self, buttons: Optional[ButtonMap] = None) -> None:
        self.buttons = buttons
        self._queue: asyncio.Queue = asyncio.Queue()

    def push(self, event: RemoteEvent) -> None:
        self._queue.put_nowait(event)

    def press(self, key: str, kind: str = "press") -> RemoteEvent:
        """Synthesises an event for a named button and queues it.

        Looks the signature up from the button map so a simulated press is
        indistinguishable from a real one -- otherwise the simulation would
        exercise a different code path than the remote does, and would stop
        being evidence that the binding works.
        """
        signature, label = key, None
        if self.buttons is not None:
            profile = next((p for p in self.buttons if p.key == key), None)
            if profile is not None:
                signature = sorted(profile.signatures)[0]
                label = profile.label

        event = RemoteEvent(kind=kind, signature=signature, label=label)  # type: ignore[arg-type]
        self.push(event)
        return event

    async def events(self) -> AsyncIterator[RemoteEvent]:
        while True:
            yield await self._queue.get()


class ReplaySource(EventSource):
    """Replays a `harmony-receiver capture` JSONL file as live events.

    The recorded packets are decoded through the same protocol parser and
    press tracker the radio path uses, driven by a clock that follows the
    file's own timestamps. So a replay reproduces real press/repeat/release
    timing -- including holds -- rather than a stream of synthetic presses
    that would never exercise the hold logic.
    """

    def __init__(self, path: str | Path, speed: float = 1.0, loop_forever: bool = False) -> None:
        self.path = Path(path)
        self.speed = speed
        self.loop_forever = loop_forever
        self._closed = False

    def _packets(self) -> list[dict[str, Any]]:
        records = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("type") == "packet":
                    records.append(record)
        return records

    async def events(self) -> AsyncIterator[RemoteEvent]:
        packets = self._packets()
        if not packets:
            logger.warning("%s contains no packets to replay", self.path)
            return

        while not self._closed:
            now = 0.0
            tracker = PressTracker(clock=lambda: now)
            previous = packets[0]["t"]

            for record in packets:
                if self._closed:
                    return
                gap = (record["t"] - previous) / max(self.speed, 0.01)
                if gap > 0:
                    await asyncio.sleep(gap)
                previous = record["t"]
                now = record["t"]

                frame = parse_frame(bytes.fromhex(record["raw"]))
                if frame is None:
                    continue
                for event in tracker.feed(frame):
                    yield event

            # Run the clock past the release timeout so the final press of
            # the file is released rather than left hanging.
            now = previous + 10.0
            for event in tracker.tick():
                yield event

            if not self.loop_forever:
                return

    async def close(self) -> None:
        self._closed = True


#: How long `RadioSource.close` waits for the polling thread to notice
#: `should_stop` before giving up on releasing the radio's pins. Comfortably
#: above a dwell window (tens of milliseconds) so a normal stop never hits
#: it; only there to bound the wait if the thread is somehow wedged.
RADIO_JOIN_TIMEOUT = 2.0


class RadioSource(EventSource):
    """The real receiver, polled on a thread.

    `HarmonyReceiver.events()` is a blocking generator built around SPI
    polling and sleeps, so it cannot run on the event loop -- it would stall
    the web server and every backend along with it. It runs on its own
    thread and hands events across with `call_soon_threadsafe`.
    """

    def __init__(self, receiver: Any, **sniff_kwargs: Any) -> None:
        self.receiver = receiver
        self.sniff_kwargs = sniff_kwargs
        self._queue: asyncio.Queue = asyncio.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _pump(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            for event in self.receiver.events(should_stop=self._stop.is_set, **self.sniff_kwargs):
                if self._stop.is_set():
                    return
                loop.call_soon_threadsafe(self._queue.put_nowait, event)
        except Exception:
            logger.exception("Radio source stopped unexpectedly")

    async def events(self) -> AsyncIterator[RemoteEvent]:
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._pump, args=(loop,), name="harmony-radio", daemon=True)
        self._thread.start()
        while not self._stop.is_set():
            yield await self._queue.get()

    async def close(self) -> None:
        # `should_stop` (wired to this flag in `_pump`) gives `sniff()` a
        # cancellation point, so the thread normally exits within one dwell
        # window -- tens of milliseconds.
        self._stop.set()

        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, RADIO_JOIN_TIMEOUT)
            if thread.is_alive():
                # Releasing the pins while the thread may still be mid-SPI-
                # transaction is what used to crash it with 'NoneType' object
                # has no attribute 'value' -- deiniting a pin out from under
                # a read in progress. Leaking the pins is the lesser evil: a
                # later restart failing with "pin in use" is at least an
                # honest error, rather than a wedged FT232H handle.
                logger.warning("Radio thread did not stop in time; leaving its pins claimed")
                return

        # Hand the FT232H pins back. Without this a hub restarted from the
        # settings page would fail its second start on "pin in use", which
        # looks exactly like the radio having been unplugged.
        radio = getattr(self.receiver, "radio", None)
        if radio is not None:
            try:
                from harmony_receiver.radio import release_radio

                release_radio(radio)
            except Exception:
                logger.debug("Radio would not release", exc_info=True)


def build_source(settings: "HubSettings") -> Optional[EventSource]:
    """The event source these settings describe, or None for 'none'.

    Raises if the settings cannot produce one -- a missing address, a radio
    that will not open. Callers decide what that means: the running hub turns
    it into a visible failure state, and the settings dry-run turns it into a
    message next to the field that caused it.

    Shared by both so that "will these settings work?" exercises the same
    code path as actually starting, rather than a second implementation that
    drifts from this one and answers a subtly different question.
    """
    if settings.source == "replay":
        if not settings.replay_path:
            raise ValueError("source='replay' needs a capture file")
        if not Path(settings.replay_path).is_file():
            raise FileNotFoundError(f"no capture file at {settings.replay_path}")
        return ReplaySource(
            settings.replay_path,
            speed=settings.replay_speed,
            loop_forever=settings.replay_loop,
        )

    if settings.source == "radio":
        # Imported here, not at module scope: the radio pulls in Blinka and
        # the FT232H stack, which must not be a prerequisite for running the
        # hub against a replay or for importing this module in a test.
        from harmony_receiver.radio import create_radio
        from harmony_receiver.receiver import HarmonyReceiver

        if not settings.address:
            raise ValueError("source='radio' needs the remote's network address")

        radio = create_radio(settings.csn_pin, settings.ce_pin)
        receiver = HarmonyReceiver(radio, bytes.fromhex(settings.address))
        return RadioSource(
            receiver,
            start_channel=settings.channel,
            probe_interval=settings.probe_interval,
            silent=not settings.allow_ack,
        )

    return None
