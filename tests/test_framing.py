"""`framing.find_addresses` against synthesised promiscuous captures.

There is no live radio in CI, so these build a capture the way the real
hardware would deliver one: a genuine Enhanced ShockBurst frame (address +
PCF + payload + real CRC-16), byte-reversed to on-air order, buried at a
deliberately non-byte-aligned bit offset inside some junk. If the search
recovers the exact address that was encoded, the bit-level framing math is
correct independent of any actual radio.
"""

from __future__ import annotations

from harmony_receiver.framing import _bits_to_int, _bytes_to_bits, _crc16_extend, find_addresses

CRC_INIT = 0xFFFF


def _encode_frame(address: bytes, payload: bytes) -> list[int]:
    """address+PCF+payload+CRC16 as on-air bits (address reversed to MSB-first)."""
    on_air_address = bytes(reversed(address))
    length_field = [(len(payload) >> i) & 1 for i in range(5, -1, -1)]
    pid = [0, 0]
    no_ack = [0]
    pcf = length_field + pid + no_ack

    bits = _bytes_to_bits(on_air_address) + pcf + _bytes_to_bits(payload)
    crc = _crc16_extend(CRC_INIT, bits)
    bits += [(crc >> i) & 1 for i in range(15, -1, -1)]
    return bits


def _capture_with_frame(address: bytes, payload: bytes, *, misalign: int = 3, junk_before: bytes = b"\xAA") -> bytes:
    """Wraps an encoded frame in enough junk to look like a real capture."""
    frame_bits = _bytes_to_bits(junk_before) + _encode_frame(address, payload)
    bits = [1] * (misalign % 8) + frame_bits if misalign else frame_bits
    # pad to a whole number of bytes, plus a spare trailing byte the way a
    # capture that kept listening a little longer than the frame would.
    bits += [0] * (-len(bits) % 8) + [0] * 8
    return bytes(_bits_to_int(bits[i:i + 8]) for i in range(0, len(bits), 8))


ADDRESS = bytes.fromhex("00129BFCB6")  # this project's discovery ("zeroed") address
TICK_PAYLOAD = bytes([0x40, 0x04, 0x4C, 0x00, 0x00])
BUTTON_PAYLOAD = bytes([0x17, 0xC3, 0xE9, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x3D])


def test_recovers_a_byte_aligned_frame():
    capture = _capture_with_frame(ADDRESS, BUTTON_PAYLOAD, misalign=0)
    assert ADDRESS in find_addresses(capture)


def test_recovers_a_bit_misaligned_frame():
    """The realistic case: a promiscuous capture rarely starts on a byte boundary."""
    capture = _capture_with_frame(ADDRESS, BUTTON_PAYLOAD, misalign=3)
    assert ADDRESS in find_addresses(capture)


def test_recovers_a_short_tick_payload():
    capture = _capture_with_frame(ADDRESS, TICK_PAYLOAD, misalign=5)
    assert ADDRESS in find_addresses(capture)


def test_recovers_a_nonzero_assigned_byte():
    """The session address (assigned LSB, not the zeroed discovery one) recovers the same way."""
    assigned = bytes.fromhex("17129BFCB6")
    capture = _capture_with_frame(assigned, BUTTON_PAYLOAD, misalign=2)
    assert assigned in find_addresses(capture)


def test_pure_noise_yields_no_addresses():
    """Random bytes essentially never satisfy a CRC-16 by chance in a short capture."""
    noise = bytes(range(32))
    assert find_addresses(noise) == []


def test_a_corrupted_frame_is_not_recovered():
    capture = bytearray(_capture_with_frame(ADDRESS, BUTTON_PAYLOAD, misalign=3))
    # Flip a bit inside the payload -- everything from there on fails CRC.
    capture[6] ^= 0x01
    assert ADDRESS not in find_addresses(bytes(capture))


def test_two_distinct_frames_in_one_capture_both_recover():
    """One capture can hold consecutive frames -- both should be found independently."""
    first = _capture_with_frame(ADDRESS, TICK_PAYLOAD, misalign=1, junk_before=b"")
    second = _capture_with_frame(ADDRESS, BUTTON_PAYLOAD, misalign=0, junk_before=b"\x55")
    capture = first + second
    assert find_addresses(capture).count(ADDRESS) >= 2
