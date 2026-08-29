"""RadioSource: the thread that bridges the blocking receiver into the loop.

Covers `close()`'s two edges -- a thread that notices `should_stop` and one
that doesn't -- since getting this wrong either wedges the FT232H handle
(deiniting pins under a live SPI transaction) or leaks a thread that never
lets go of the radio.
"""

from __future__ import annotations

import asyncio
import threading
import time

from harmony_hub import sources as sources_module
from harmony_hub.sources import RadioSource


class FakeRadio:
    """Stands in for the nRF24 handle; only its identity matters here."""


class CooperativeReceiver:
    """A fake receiver whose `events()` notices `should_stop` promptly, the
    way `HarmonyReceiver.sniff()` does once given a cancellation point.
    """

    def __init__(self) -> None:
        self.radio = FakeRadio()
        self.calls: list[dict] = []

    def events(self, should_stop=None, **kwargs):
        self.calls.append(kwargs)
        while should_stop is None or not should_stop():
            time.sleep(0.01)


class StubbornReceiver:
    """A fake receiver whose `events()` ignores `should_stop` -- standing in
    for a thread wedged inside one long-running SPI call.
    """

    def __init__(self) -> None:
        self.radio = FakeRadio()

    def events(self, should_stop=None, **kwargs):
        while True:
            time.sleep(0.01)


def _start_pump(source: RadioSource) -> threading.Thread:
    """Starts the polling thread the way `RadioSource.events()` does, without
    going through the async generator (which never yields here -- these fake
    receivers produce no events, only prove the shutdown handshake).
    """
    loop = asyncio.get_running_loop()
    thread = threading.Thread(target=source._pump, args=(loop,), name="test-harmony-radio", daemon=True)
    source._thread = thread
    thread.start()
    return thread


async def test_close_forwards_should_stop_and_the_sniff_kwargs():
    receiver = CooperativeReceiver()
    source = RadioSource(receiver, probe_interval=1.5, silent=True)
    _start_pump(source)

    await source.close()

    assert receiver.calls == [{"probe_interval": 1.5, "silent": True}]


async def test_close_joins_a_cooperative_thread_then_releases_the_radio(monkeypatch):
    released = []
    import harmony_receiver.radio as radio_module

    monkeypatch.setattr(radio_module, "release_radio", lambda radio: released.append(radio))
    monkeypatch.setattr(sources_module, "RADIO_JOIN_TIMEOUT", 2.0)

    receiver = CooperativeReceiver()
    source = RadioSource(receiver)
    thread = _start_pump(source)

    await source.close()

    assert released == [receiver.radio]
    assert not thread.is_alive()
    assert source._thread is None


async def test_close_gives_up_and_leaves_the_radio_claimed_if_the_thread_wont_stop(monkeypatch):
    released = []
    import harmony_receiver.radio as radio_module

    monkeypatch.setattr(radio_module, "release_radio", lambda radio: released.append(radio))
    # A real wait uses RADIO_JOIN_TIMEOUT (2s); shortened so the stubborn
    # thread's non-cooperation doesn't make the test itself slow.
    monkeypatch.setattr(sources_module, "RADIO_JOIN_TIMEOUT", 0.05)

    receiver = StubbornReceiver()
    source = RadioSource(receiver)
    thread = _start_pump(source)

    await source.close()

    assert released == []
    assert thread.is_alive()
