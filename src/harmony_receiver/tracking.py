"""Turns a stream of raw Harmony frames into press / repeat / release events.

The remote reports button state the way a USB HID device does: a report
naming the button that is down, then an all-zero report when nothing is. So
a release is an explicit packet, not a guess.

A timeout still backs it up. The link drops packets -- the radio is on one
channel, the FIFO is three deep, and a probe or a channel change can eat a
packet -- and a lost release packet would otherwise leave a button stuck
down forever. Missing a release is far more damaging than reporting one a
fraction of a second early, so silence eventually ends a press too.

Kept free of any radio dependency so it can be tested against synthetic
timelines with a fake clock.
"""

from __future__ import annotations

import time
from typing import Callable, Iterator, Optional

from .events import RemoteEvent
from .protocol import Frame

# Three missed 100ms ticks. Long enough that a packet lost to a busy radio or
# a slow USB round trip does not fake a release, short enough to stay well
# clear of the ~1s idle cadence that genuinely means "nothing is held".
DEFAULT_RELEASE_TIMEOUT = 0.35

Clock = Callable[[], float]


class PressTracker:
    """Folds frames into button events, holding the currently-pressed button.

    Call `feed()` for every frame received and `tick()` periodically even
    when nothing arrives, so the release timeout can fire.
    """

    def __init__(
        self,
        resolve_label: Optional[Callable[[str], Optional[str]]] = None,
        release_timeout: float = DEFAULT_RELEASE_TIMEOUT,
        clock: Clock = time.monotonic,
    ) -> None:
        self._resolve_label = resolve_label or (lambda signature: None)
        self._release_timeout = release_timeout
        self._clock = clock

        self._active: Optional[Frame] = None
        self._last_activity: float = 0.0

    @property
    def active_signature(self) -> Optional[str]:
        """The signature of the button currently held, if any."""
        return self._active.signature if self._active else None

    def _event(self, kind: str, frame: Frame, raw: Optional[bytes] = None) -> RemoteEvent:
        # A learned profile wins over the HID table: it is a human saying
        # what this button is called on this particular remote.
        label = self._resolve_label(frame.signature) or frame.label
        return RemoteEvent(
            kind=kind,  # type: ignore[arg-type]
            signature=frame.signature,
            report=frame.kind,
            usage=frame.usage,
            label=label,
            raw=raw if raw is not None else frame.payload,
            channel=frame.channel,
        )

    def _release(self, raw: Optional[bytes] = None) -> Iterator[RemoteEvent]:
        if self._active is None:
            return
        released, self._active = self._active, None
        yield self._event("release", released, raw)

    def feed(self, frame: Frame) -> Iterator[RemoteEvent]:
        """Yields the events (if any) implied by one received frame."""
        now = self._clock()

        if frame.kind == "tick":
            # Ticks carry no identity of their own; they only prove the
            # button named by the last report is still down.
            if self._active is not None:
                self._last_activity = now
                yield self._event("repeat", self._active, raw=frame.payload)
            return

        if not frame.is_button:
            return  # status traffic: not a button, and not evidence of one

        self._last_activity = now

        if frame.is_release:
            yield from self._release(raw=frame.payload)
            return

        if self._active is not None:
            if self._active.usage == frame.usage and self._active.kind == frame.kind:
                return  # the same report repeated; still one press
            # A different button arrived without the first one's release.
            yield from self._release()

        self._active = frame
        yield self._event("press", frame)

    def tick(self) -> Iterator[RemoteEvent]:
        """Yields a release once a held button's traffic has gone quiet."""
        if self._active is None:
            return
        if self._clock() - self._last_activity <= self._release_timeout:
            return
        yield from self._release()
