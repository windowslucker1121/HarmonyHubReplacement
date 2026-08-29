"""The Harmony RF24 protocol: constants, addressing, and pure frame parsing.

This module has no hardware dependencies and can be imported and unit tested
without an nRF24L01+ radio attached. Based on the reverse engineering in
https://github.com/joakimjalden/Harmoino plus decoding worked out against
real captures from this project's Hub/remote pair.

Addressing
----------
Every packet described here travels *remote -> Hub*; the Hub is the passive
side that waits. A 40-bit network address ending in LSB ``E`` gives the
remote two addresses to talk to, used in sequence:

===============  ==============  ==========  =================================
address          payload[0]      pipe        role
===============  ==============  ==========  =================================
``<network>00``  ``E``           discovery   first packets of a press
``<network>E``   ``0x00``        session     every packet after
===============  ==============  ==========  =================================

Both carry the same report body, so button identity is read from either.

Payload layout
--------------
Byte 0 is addressing bookkeeping and byte 9 is the checksum. Everything that
matters is in between::

    byte 1     report id  (which kind of report this is)
    bytes 2-4  report body
    bytes 5-8  padding, always zero in every capture so far

The remote sends **standard USB HID reports**, so a button's meaning can be
decoded rather than guessed. See `hid` for the usage tables and the captured
evidence behind that claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from .hid import consumer_name, keyboard_name

# The 12 discrete channels a Harmony Hub may listen on.
HARMONY_CHANNELS = [5, 8, 14, 17, 32, 35, 41, 44, 62, 65, 71, 74]

# Shared global pairing address (0xBB0ADCA575), in the driver's LSB-first byte order.
PAIRING_ADDRESS = b"\x75\xA5\xDC\x0A\xBB"

# Fixed handshake payloads that trigger a Harmony Hub into revealing the
# unique per-remote network address via an ACK payload.
PAIR_MESSAGE = bytes([242, 95, 1, 225, 154, 157, 218, 83, 40, 64, 30, 4, 2, 7, 12, 0, 0, 0, 0, 0, 102, 100])
PING_MESSAGE = bytes([242, 64, 1, 225, 236])

# RX pipe assignments. Pipes 2-5 on an nRF24L01+ share pipe 1's upper four
# address bytes and only carry their own LSB, so the discovery address must be
# opened on pipe 1 for the session address on pipe 2 to inherit from it.
DISCOVERY_PIPE = 1
SESSION_PIPE = 2

# Observed remote transmit cadences (Harmoino, matching this project's captures).
HELD_TICK_INTERVAL = 0.1  # 5-byte packet every ~100ms while a button is held
IDLE_TICK_INTERVAL = 1.0  # every ~1s for ~30s once nothing is held

COMMAND_LENGTH = 10
TICK_LENGTH = 5

# Report ids seen in byte 1 of a 10-byte packet.
REPORT_KEYBOARD = 0xC1  # HID keyboard page: [modifier, keycode, 0]
REPORT_CONSUMER = 0xC3  # HID consumer page: [usage low, usage high, 0]
REPORT_STATUS = 0x4F  # not a button; carries session/state values
REPORT_TICK = 0x40  # byte 1 of the 5-byte keepalive

# Bytes 1..4 identify a report; used as the stable key for learned profiles.
SIGNATURE_SLICE = slice(1, 5)
BODY_SLICE = slice(2, 5)

FrameKind = Literal["keyboard", "consumer", "status", "tick", "unknown"]


@dataclass(frozen=True)
class Frame:
    """One checksum-valid packet lifted off the air, decoded as far as possible."""

    kind: FrameKind
    payload: bytes
    signature: str  # hex of bytes 1..4, the stable identity of this report
    usage: Optional[int] = None  # HID usage code, for keyboard/consumer reports
    label: Optional[str] = None  # human name for that usage, when known
    is_release: bool = False  # an all-zero report body means "nothing held"
    first_packet: bool = False  # arrived on the discovery address
    pipe: Optional[int] = None
    channel: Optional[int] = None

    @property
    def is_button(self) -> bool:
        """Whether this frame reports button state at all (press or release)."""
        return self.kind in ("keyboard", "consumer")

    @property
    def name(self) -> str:
        """The friendly button name if known, else something identifying."""
        if self.label:
            return self.label
        if self.usage is not None:
            return f"{self.kind} usage 0x{self.usage:02X}"
        return f"<{self.signature}>"


def validate_checksum(payload: bytes) -> bool:
    """The sum of all bytes in a valid Harmony payload, modulo 256, is always zero."""
    if not payload:
        return False
    return (sum(payload) % 256) == 0


def discovery_address(network_address: bytes) -> bytes:
    """The ``<network>00`` address the remote opens a fresh press against."""
    return b"\x00" + network_address[1:]


def session_address(network_address: bytes) -> bytes:
    """The full ``<network>E`` address the remote uses once the Hub has answered."""
    return bytes(network_address)


def parse_frame(payload: bytes, pipe: Optional[int] = None, channel: Optional[int] = None) -> Optional[Frame]:
    """Decodes a raw radio payload, or returns None if it isn't a valid Harmony packet."""
    if not validate_checksum(payload):
        return None
    if len(payload) not in (COMMAND_LENGTH, TICK_LENGTH):
        return None

    signature = payload[SIGNATURE_SLICE].hex().upper()
    # A first packet announces the network address LSB in byte 0; every later
    # packet zeroes it. That is more reliable than the pipe number, which
    # depends on how the pipes happen to be opened.
    first_packet = payload[0] != 0x00
    common = {
        "payload": bytes(payload),
        "signature": signature,
        "first_packet": first_packet,
        "pipe": pipe,
        "channel": channel,
    }

    if len(payload) == TICK_LENGTH:
        return Frame(kind="tick", **common)

    report_id = payload[1]
    body = payload[BODY_SLICE]
    is_release = not any(body)

    if report_id == REPORT_KEYBOARD:
        # [modifier, keycode, 0] -- the modifier byte has been 0x00 in every
        # capture, but it is kept out of the usage so a future modified key
        # still decodes to the right base key.
        usage = payload[3]
        return Frame(kind="keyboard", usage=usage, label=keyboard_name(usage), is_release=is_release, **common)

    if report_id == REPORT_CONSUMER:
        # 16-bit usage, little-endian: E9 00 -> 0x00E9 (Volume Up).
        usage = int.from_bytes(payload[2:4], "little")
        return Frame(kind="consumer", usage=usage, label=consumer_name(usage), is_release=is_release, **common)

    if report_id == REPORT_STATUS:
        return Frame(kind="status", is_release=is_release, **common)

    return Frame(kind="unknown", is_release=is_release, **common)
