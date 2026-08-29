"""Events the hub publishes about itself, and the broker that fans them out.

Distinct from `harmony_receiver.events`, which describes what the *remote*
did. These describe what the *hub* did in response -- which button arrived,
which scene it switched to, which command ran and whether it worked. They
are what the live view in the UI renders, and what makes a misbehaving
binding diagnosable without reading logs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import AsyncIterator, List, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("HUB.events")

#: "hub" is the runtime itself starting, stopping or failing -- distinct from
#: "status", which is the running hub reporting on something it did. The app
#: refetches the settings page's state on one and only logs the other.
#: "update" is progress from a remote deploy in flight (staging, installing
#: dependencies, the smoke test) -- shown in the same Live log as everything
#: else, since it hides by exclusion rather than inclusion (see
#: `HubEvent.facets` on the app side) and a new event type should default to
#: visible.
EventType = Literal["button", "scene", "action", "status", "hub", "update"]

# Enough to absorb a burst of held-button repeats without a slow consumer
# stalling the engine. Past this a subscriber is dropped rather than allowed
# to apply backpressure to the remote's event stream.
QUEUE_LIMIT = 256


class HubEvent(BaseModel):
    """One thing that happened, flat enough to render without unwrapping."""

    type: EventType
    at: datetime = Field(default_factory=datetime.now)

    button: Optional[str] = None  # button key, or a raw signature if unknown
    label: Optional[str] = None  # human-readable button name
    phase: Optional[str] = None  # press / repeat / hold / release
    scene: Optional[str] = None
    action: Optional[str] = None  # e.g. "living_room_tv.volume_up"
    ok: Optional[bool] = None
    detail: Optional[str] = None

    def __str__(self) -> str:
        bits = [self.type]
        for value in (self.button, self.phase, self.scene, self.action, self.detail):
            if value:
                bits.append(str(value))
        return " ".join(bits)


class EventBroker:
    """Fans hub events out to any number of listeners, none of which can block the hub.

    Each subscriber gets its own bounded queue. A subscriber that stops
    draining -- a browser tab that froze, a websocket on a bad connection --
    loses its oldest events instead of slowing everything down; the remote
    staying responsive matters more than one stale listener seeing a
    complete history.
    """

    def __init__(self, history: int = 200) -> None:
        self._queues: List[asyncio.Queue] = []
        self._history: List[HubEvent] = []
        self._history_limit = history

    @property
    def history(self) -> List[HubEvent]:
        """Recent events, so a page that just opened is not blank."""
        return list(self._history)

    def publish(self, event: HubEvent) -> None:
        self._history.append(event)
        del self._history[: -self._history_limit]

        for queue in list(self._queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()  # drop the oldest, keep the newest
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    logger.debug("Dropping event for a stalled subscriber")

    async def subscribe(self) -> AsyncIterator[HubEvent]:
        """Yields events as they are published, starting from now."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._queues.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._queues.remove(queue)
