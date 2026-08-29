"""Recovers Harmony RF24 addresses from a promiscuous capture, without a Hub.

Normal operation matches complete addresses in hardware: the nRF24L01+ only
ever hands you a payload once its address and CRC have both checked out.
That is exactly what makes learning a *brand new* address hard -- there is
no address to match against yet.

The trick (`address_width = 2` is technically illegal per the datasheet, and
is Travis Goodspeed's promiscuous-mode finding for this chip) makes the
radio latch onto any two-byte prefix that looks like an address, regardless
of what follows. That turns "capture one real packet" into "capture 32
bytes of mostly noise, maybe containing a real packet at some bit offset".
This module is the software side of the trade: it brute-forces every
possible bit offset in a raw capture and keeps only the framings whose
trailing bits are a genuine CRC-16 of everything before them. See
`pairing.sniff_network_address` for how a caller uses this against a live
radio; nothing here touches hardware, so it is fully unit-testable against
synthesised captures.

Approach and constants (address width, CRC parameters, preamble polarity,
payload length bounds) follow LeoKlaus/Equilibrium's
`rf_manager/discover_remote_address.py`, the first published prior art for
doing this against a Harmony remote.
"""

from __future__ import annotations

# Preamble polarity is unit-specific -- try both until one locks.
PREAMBLES = (0xAA, 0x55)

_ADDRESS_BYTES = 5
_ADDRESS_BITS = _ADDRESS_BYTES * 8
_PCF_BITS = 9  # 6-bit payload length + 2-bit PID + 1-bit NO_ACK
_PREFIX_BITS = _ADDRESS_BITS + _PCF_BITS
_CRC_POLY = 0x1021
_CRC_INIT = 0xFFFF
_CRC_BITS = 16

# The remote's payloads are 5 (tick) or 10 (button) bytes; a couple of
# bytes either side of that costs almost nothing to also search and guards
# against an off-by-one in this project's own understanding of the framing.
_MIN_PAYLOAD_LEN = 4
_MAX_PAYLOAD_LEN = 11

# Bit offsets to try before giving up on one capture. 192 covers a full
# 24-byte capture's worth of starting positions, matching Equilibrium.
_MAX_START_BIT = 192


def _bytes_to_bits(data: bytes) -> list[int]:
    return [(byte >> i) & 1 for byte in data for i in range(7, -1, -1)]


def _bits_to_int(bits: list[int]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return value


def _crc16_extend(crc: int, bits: list[int]) -> int:
    """Feeds bits into an in-progress CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF).

    Verified against the reference vector CRC16("123456789") == 0x29B1.
    Incremental so a search over many candidate payload lengths does not
    redo the CRC work in common with a shorter candidate already tried.
    """
    for bit in bits:
        msb = (crc >> 15) & 1
        crc = (crc << 1) & 0xFFFF
        if msb ^ bit:
            crc ^= _CRC_POLY
    return crc


def find_addresses(capture: bytes) -> list[bytes]:
    """Every address a raw promiscuous capture's CRC-16 actually vouches for.

    Searches every bit offset, not just byte-aligned ones -- address+PCF is
    49 bits, so a real frame is very unlikely to start on a byte boundary
    of the capture buffer. A hit here means the on-air CRC-16 validated
    over a specific address + control field + payload, which is strong
    enough that a single hit is meaningful, though `pairing.py` still asks
    for the same address to show up more than once before trusting it --
    two-byte address matching produces enough of its own false starts that
    a lone CRC pass is not proof by itself.

    Returns addresses in the byte order `protocol.py` uses elsewhere
    (LSB first), reversing the on-air MSB-first order.
    """
    bits = _bytes_to_bits(capture)
    total = len(bits)
    found = []
    for start in range(min(_MAX_START_BIT, total)):
        if start + _PREFIX_BITS > total:
            break
        crc = _crc16_extend(_CRC_INIT, bits[start:start + _PREFIX_BITS])
        pos = start + _PREFIX_BITS
        for payload_len in range(_MAX_PAYLOAD_LEN + 1):
            if pos + _CRC_BITS > total:
                break
            if payload_len >= _MIN_PAYLOAD_LEN and crc == _bits_to_int(bits[pos:pos + _CRC_BITS]):
                address_bits = bits[start:start + _ADDRESS_BITS]
                address = bytes(_bits_to_int(address_bits[i:i + 8]) for i in range(0, _ADDRESS_BITS, 8))
                found.append(bytes(reversed(address)))
            if pos + 8 > total:
                break
            crc = _crc16_extend(crc, bits[pos:pos + 8])
            pos += 8
    return found
