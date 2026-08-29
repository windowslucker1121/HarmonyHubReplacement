"""`HubService.stop()`'s shutdown ordering.

Cancelling a pump task only *requests* cancellation -- it does not block
until the task has actually unwound. Closing sources (which, for the radio,
releases the FT232H pins) before that cancellation has actually landed races
a pump that might still be mid-`engine.handle()` against the teardown of the
backends it's calling into.
"""

from __future__ import annotations

import asyncio

from harmony_hub.events import EventBroker
from harmony_hub.models import HubConfig
from harmony_hub.service import HubService
from harmony_hub.settings import HubSettings
from harmony_hub.sources import EventSource
from harmony_receiver.profiles import ButtonMap


class OrderedSource(EventSource):
    """Records when its pump task actually finishes unwinding, vs. when
    `close()` is called, so the two can be checked against each other.
    """

    def __init__(self, order: list) -> None:
        self.order = order

    async def events(self):
        try:
            while True:
                await asyncio.sleep(3600)
                yield None  # pragma: no cover - never reached
        finally:
            self.order.append("pump-finished")

    async def close(self) -> None:
        self.order.append("source-closed")


async def test_stop_waits_for_a_cancelled_pump_to_finish_before_closing_its_source():
    order: list = []
    service = HubService(HubSettings(source="none"), HubConfig(), ButtonMap(), EventBroker())
    await service.start()

    source = OrderedSource(order)
    service._sources.append(source)
    task = asyncio.create_task(service._pump(source))
    service._tasks.append(task)
    # Let the task actually reach `await asyncio.sleep(3600)` before cancelling
    # it, so `stop()` is racing a task that is genuinely still running.
    await asyncio.sleep(0)

    await service.stop()

    assert order == ["pump-finished", "source-closed"]
