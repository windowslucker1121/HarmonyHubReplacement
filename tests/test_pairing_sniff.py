"""`pairing.sniff_network_address` -- hub-less discovery, against a FakeRadio.

Covers the two phases separately from real hardware, the same way
`test_receiver_sniff.py` covers `HarmonyReceiver.sniff()`'s cancellation
point and `test_framing.py` covers the CRC-16 recovery math on its own:
promiscuous capture -> candidate address, then candidate -> confirmed via a
throwaway `HarmonyReceiver` on matched addressing. `FakeRadio` here plays
both roles, distinguished by `address_length` (2 while promiscuous, 5 once
`reset_radio` has run), the same signal the real driver would carry.
"""

from __future__ import annotations

import pytest

from harmony_receiver.framing import _bits_to_int, _bytes_to_bits, _crc16_extend
from harmony_receiver.pairing import PairingCancelled, PairingTimeout, sniff_network_address
from harmony_receiver.protocol import HARMONY_CHANNELS

SHARED = bytes.fromhex("129BFCB6")  # this project's usual test address, minus its LSB
ASSIGNED_LSB = 0x17  # matches the "17129BFCB6" address used throughout the other tests


def _encode_capture(address: bytes, payload: bytes) -> bytes:
    """A 32-byte promiscuous capture containing one genuine frame at a fixed offset.

    Kept deliberately simple (byte-aligned, one fixed preamble byte) --
    `test_framing.py` already proves the bit-level search handles
    misalignment; this only needs *a* recoverable frame; where within the 32
    bytes is not the point of these tests.
    """
    on_air_address = bytes(reversed(address))
    length_field = [(len(payload) >> i) & 1 for i in range(5, -1, -1)]
    pcf = length_field + [0, 0, 0]
    bits = _bytes_to_bits(b"\xAA" + on_air_address) + pcf + _bytes_to_bits(payload)
    crc = _crc16_extend(0xFFFF, bits[8:])  # skip the leading junk byte
    bits += [(crc >> i) & 1 for i in range(15, -1, -1)]
    bits += [0] * (-len(bits) % 8)
    capture = bytes(_bits_to_int(bits[i:i + 8]) for i in range(0, len(bits), 8))
    return capture.ljust(32, b"\x00")[:32]


def _checksum_valid_payload(first_byte: int) -> bytes:
    """A 10-byte application-layer frame `parse_frame` accepts (see protocol.py)."""
    payload = bytearray([first_byte, 0xC3, 0xE9, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    payload.append((-sum(payload)) % 256)
    return bytes(payload)


class FakeRadio:
    """Enough of the nRF24 surface for both phases of `sniff_network_address`.

    `address_length` doubles as the phase switch: `set_promiscuous` sets it
    to 2, `reset_radio` (called between phases) sets it back to 5 -- exactly
    the signal the real driver carries, so nothing extra is needed to tell
    the two FIFOs apart.
    """

    def __init__(self) -> None:
        self.listen = False
        self.channel = None
        self.ack = None
        self.auto_ack = None
        self.crc = None
        self.dynamic_payloads = None
        self.payload_length = None
        self.address_length = None
        self.pipe = 1
        self.opened_rx_pipes: list[tuple[int, bytes]] = []
        # Consumed regardless of channel/preamble -- these tests only care
        # that *some* combination catches them, not which.
        self.promiscuous_fifo: list[bytes] = []
        # Keyed by channel: `HarmonyReceiver.sniff()` only "finds" traffic
        # on whatever channel it happens to be tuned to at the time.
        self.matched_fifo: "dict[int, list[bytes]]" = {}

    def open_rx_pipe(self, pipe: int, address: bytes) -> None:
        self.opened_rx_pipes.append((pipe, address))

    def open_tx_pipe(self, address: bytes) -> None:
        pass

    def _active_fifo(self) -> list:
        if self.address_length == 2:
            return self.promiscuous_fifo
        return self.matched_fifo.setdefault(self.channel, [])

    def available(self) -> bool:
        return bool(self._active_fifo())

    def read(self, length=None):
        fifo = self._active_fifo()
        return fifo.pop(0) if fifo else None


def test_recovers_a_nonzero_address_directly_no_resync_needed():
    """A candidate whose LSB was already non-zero in the promiscuous capture
    needs no correction: the confirmed address should come back unchanged."""
    address = bytes([ASSIGNED_LSB]) + SHARED
    capture = _encode_capture(address, _checksum_valid_payload(0x00))

    radio = FakeRadio()
    radio.promiscuous_fifo = [capture, capture]  # _CONFIRMATIONS_NEEDED == 2
    found_channel = HARMONY_CHANNELS[0]
    radio.matched_fifo[found_channel] = [_checksum_valid_payload(0x00)]

    result_address, result_channel = sniff_network_address(radio, timeout_sec=5)

    assert result_address == address
    assert result_channel == found_channel


def test_falls_back_to_placeholder_zero_and_resyncs_from_the_first_real_packet():
    """When every promiscuous sighting had a zero LSB, the placeholder
    "00"+shared candidate is used -- and the verify phase's own
    `HarmonyReceiver._resync_from_first_packet` is what corrects it, not a
    second discovery pass. This is the resync-only design: no 255-candidate
    sweep, because this path already fixes it from real traffic."""
    placeholder = bytes([0x00]) + SHARED
    capture = _encode_capture(placeholder, _checksum_valid_payload(0x00))

    radio = FakeRadio()
    radio.promiscuous_fifo = [capture, capture]
    found_channel = HARMONY_CHANNELS[0]
    # A first packet (non-zero byte 0) announcing the true LSB -- exactly
    # what `_resync_from_first_packet` watches for.
    radio.matched_fifo[found_channel] = [_checksum_valid_payload(ASSIGNED_LSB)]

    result_address, result_channel = sniff_network_address(radio, timeout_sec=5)

    assert result_address == bytes([ASSIGNED_LSB]) + SHARED
    assert result_channel == found_channel


def test_cancelled_during_the_promiscuous_phase_raises_promptly():
    radio = FakeRadio()  # never yields a capture

    with pytest.raises(PairingCancelled):
        sniff_network_address(radio, timeout_sec=30, should_stop=lambda: True)


def test_no_candidate_recovered_before_the_timeout_raises_pairing_timeout():
    radio = FakeRadio()  # promiscuous_fifo stays empty the whole time

    with pytest.raises(PairingTimeout):
        sniff_network_address(radio, timeout_sec=0.05)


def test_a_confirmed_candidate_that_never_shows_real_traffic_times_out_in_verify():
    address = bytes([ASSIGNED_LSB]) + SHARED
    capture = _encode_capture(address, _checksum_valid_payload(0x00))

    radio = FakeRadio()
    radio.promiscuous_fifo = [capture, capture]
    # matched_fifo stays empty on every channel -- the candidate is never
    # actually proven, which must fail loudly rather than return it anyway.

    with pytest.raises(PairingTimeout, match="couldn't confirm"):
        sniff_network_address(radio, timeout_sec=5, verify_timeout_sec=0.05)


def test_progress_callback_is_told_about_both_phases():
    address = bytes([ASSIGNED_LSB]) + SHARED
    capture = _encode_capture(address, _checksum_valid_payload(0x00))

    radio = FakeRadio()
    radio.promiscuous_fifo = [capture, capture]
    found_channel = HARMONY_CHANNELS[0]
    radio.matched_fifo[found_channel] = [_checksum_valid_payload(0x00)]

    messages: list[str] = []
    sniff_network_address(radio, timeout_sec=5, on_progress=messages.append)

    assert any("Listening" in m for m in messages)
    assert any("candidate" in m.lower() for m in messages)
    assert any("Confirmed" in m for m in messages)
