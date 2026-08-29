"""Wires an event source to the engine: one running hub, and nothing more.

Kept separate from the web layer so the hub can be embedded in something
else -- a script, a test, a different front end -- without dragging FastAPI
along, and so the API module stays a thin translation of HTTP to method
calls.

Deliberately owns nothing that should outlive it. Configuration, the button
map and the event broker are handed in by `HubRuntime`, which keeps them
across restarts; this object is the disposable part, built when the hub
starts and thrown away when it stops.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from harmony_receiver.events import RemoteEvent
from harmony_receiver.profiles import ButtonMap

from .engine import SceneEngine
from .events import EventBroker, HubEvent
from .ir import gateway as ir_gateway
from .models import HubConfig
from .settings import HubSettings
from .sources import EventSource, ManualSource, build_source

logger = logging.getLogger("HUB.service")


class HubService:
    """A running hub: an engine, and whatever is feeding it events."""

    def __init__(
        self,
        settings: HubSettings,
        config: HubConfig,
        buttons: ButtonMap,
        broker: EventBroker,
    ) -> None:
        self.settings = settings
        self.config = config
        self.buttons = buttons
        self.broker = broker
        self.engine = SceneEngine(config, buttons, broker)

        # Always present, even alongside the radio: the UI's "try this
        # button" needs somewhere to inject a press regardless of what else
        # is running.
        self.manual = ManualSource(buttons)
        self._sources: List[EventSource] = [self.manual]
        self._tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------------

    @property
    def uses_radio(self) -> bool:
        """Whether this hub is holding the radio, and so cannot share it."""
        return self.settings.source == "radio"

    async def start(self) -> None:
        # Configured here rather than left to whichever IR device connects
        # first: `IrBackend.connect()` never raises (see its docstring), so
        # if nothing set the gateway up its `health()` would just report
        # "not configured" forever instead of the pins actually in use.
        ir_gateway.reconfigure(self.settings)
        source = build_source(self.settings)
        if source is not None:
            self._sources.append(source)
        await self.engine.start()
        for source in self._sources:
            self._tasks.append(asyncio.create_task(self._pump(source)))
        logger.info("Hub started with %d event source(s)", len(self._sources))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        # Awaited before sources are closed and the engine is torn down, so
        # nothing is still mid-`engine.handle()` while the backends it might
        # call into are being shut down underneath it.
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for source in self._sources:
            try:
                await source.close()
            except Exception:
                logger.exception("Event source %s failed to close cleanly", type(source).__name__)
        await self.engine.stop()
        ir_gateway.gateway().shutdown()

    async def _pump(self, source: EventSource) -> None:
        """Feeds one source's events into the engine until cancelled.

        A source that dies takes itself down and says so, rather than
        silently ending the stream -- a hub that has stopped hearing the
        remote looks identical to one where nobody is pressing anything.
        """
        try:
            async for event in source.events():
                await self.engine.handle(event)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.exception("Event source %s stopped", type(source).__name__)
            self.broker.publish(
                HubEvent(type="status", ok=False, detail=f"{type(source).__name__} stopped: {err}")
            )

    # ------------------------------------------------------------------

    async def apply_config(self, new_config: HubConfig) -> None:
        """Swaps in configuration that has already been persisted elsewhere."""
        self.config = new_config
        await self.engine.reload(new_config)

    def apply_buttons(self, buttons: ButtonMap) -> None:
        self.buttons = buttons
        self.engine.buttons = buttons
        self.manual.buttons = buttons

    def simulate(self, key: str, kind: str = "press") -> RemoteEvent:
        """Injects a button press as though the remote had sent it."""
        return self.manual.press(key, kind)
