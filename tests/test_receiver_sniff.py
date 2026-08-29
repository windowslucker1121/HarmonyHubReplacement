"""`HarmonyReceiver.sniff()`'s `should_stop` cancellation point.

Mirrors the precedent already established for the discovery loop in
`pairing.discover_network_address` -- `sniff()` otherwise has no way to
return, which is what let a hub shutdown wedge the FT232H handle: the pins
were released while this loop was still mid-SPI-transaction on its thread.
"""

from __future__ import annotations

from harmony_receiver.protocol import discovery_address, session_address
from harmony_receiver.receiver import HarmonyReceiver

NETWORK_ADDRESS = bytes.fromhex("17129BFCB6")


class FakeRadio:
    """Just enough of the nRF24 interface for `sniff()` to run its loop."""

    def __init__(self) -> None:
        self.listen = False
        self.channel = None
        self.ack = None
        self.auto_ack = None
        self.opened_rx_pipes: list[tuple[int, bytes]] = []

    def open_rx_pipe(self, pipe: int, address: bytes) -> None:
        self.opened_rx_pipes.append((pipe, address))

    def available(self) -> bool:
        return False  # no packets waiting, ever


def test_should_stop_true_before_the_first_iteration_returns_immediately():
    radio = FakeRadio()
    receiver = HarmonyReceiver(radio, NETWORK_ADDRESS)

    frames = list(receiver.sniff(probe_interval=0, should_stop=lambda: True))

    assert frames == []
    # Pipes are opened before the should_stop check -- confirms this
    # actually ran the loop's setup rather than short-circuiting earlier.
    assert radio.opened_rx_pipes == [
        (1, discovery_address(NETWORK_ADDRESS)),
        (2, session_address(NETWORK_ADDRESS)),
    ]


def test_should_stop_becoming_true_mid_dwell_stops_the_loop_promptly():
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        # False on the outer-loop check, true on the very next check --
        # inside the dwell window's inner while -- so this proves that
        # inner check exists and is reached, not just the outer one.
        return calls["n"] > 1

    radio = FakeRadio()
    receiver = HarmonyReceiver(radio, NETWORK_ADDRESS)

    frames = list(receiver.sniff(probe_interval=0, should_stop=should_stop))

    assert frames == []
    assert calls["n"] >= 2
