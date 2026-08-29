"""A minimal publish/subscribe mechanism for RemoteEvents.

Lets other code react to events without needing to be the one driving
`HarmonyReceiver.events()` — e.g. an MQTT bridge and a logger can both
subscribe to the same receiver at once.
"""

from __future__ import annotations

import logging
from typing import Callable

from .events import RemoteEvent

logger = logging.getLogger("DISPATCHER")

Subscriber = Callable[[RemoteEvent], None]


class EventDispatcher:
    """Notifies subscribed callbacks whenever an event is published.

    A broken subscriber shouldn't stop other subscribers from being
    notified, so an exception raised by one callback is logged rather
    than propagated.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Subscriber) -> None:
        self._subscribers.remove(callback)

    def publish(self, event: RemoteEvent) -> None:
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:
                logger.exception("Subscriber %r raised while handling %r", callback, event)
