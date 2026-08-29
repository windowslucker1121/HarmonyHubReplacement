"""Protocol decoding checked against real payloads captured from the Hub.

These are the actual bytes recorded off the air during development. Where a
case is annotated with a button name, that name is what the operator
reported pressing at the moment of capture -- so these tests pin the decode
to observed reality, not just to itself.
"""

from __future__ import annotations

import pytest

from harmony_receiver.protocol import (
    discovery_address,
    parse_frame,
    session_address,
    validate_checksum,
)

NETWORK_ADDRESS = bytes.fromhex("17129BFCB6")

# (raw hex, kind, usage, is_release, first_packet, name)
CAPTURED = [
    # Confirmed against operator-reported button names:
    ("17C100520000000000D6", "keyboard", 0x52, False, True, "Up Arrow"),
    ("00C100510000000000EE", "keyboard", 0x51, False, False, "Down Arrow"),
    ("17C3E90000000000003D", "consumer", 0x00E9, False, True, "Volume Up"),
    ("00C3E900000000000054", "consumer", 0x00E9, False, False, "Volume Up"),
    # Release reports: same report id, empty body.
    ("00C1000000000000003F", "keyboard", 0x00, True, False, None),
    ("00C3000000000000003D", "consumer", 0x0000, True, False, None),
    # Not buttons.
    ("174F0700000000000093", "status", None, False, True, None),
    ("004F03000000000000AE", "status", None, False, False, None),
    ("004F00044C0000000061", "status", None, False, False, None),
    ("0040044C70", "tick", None, False, False, None),
    ("0040002898", "tick", None, False, False, None),
]


@pytest.mark.parametrize("raw,kind,usage,is_release,first,name", CAPTURED)
def test_captured_payloads_decode_as_expected(raw, kind, usage, is_release, first, name):
    frame = parse_frame(bytes.fromhex(raw), pipe=1 if first else 2, channel=62)

    assert frame is not None
    assert frame.kind == kind
    assert frame.usage == usage
    assert frame.is_release is is_release
    assert frame.first_packet is first
    if name is not None:
        assert frame.label == name


@pytest.mark.parametrize("raw,kind,usage,is_release,first,name", CAPTURED)
def test_every_captured_payload_has_a_valid_checksum(raw, kind, usage, is_release, first, name):
    assert validate_checksum(bytes.fromhex(raw))


def test_the_two_arrow_presses_are_distinguishable():
    """The original symptom: these two looked identical and could not be told apart."""
    up = parse_frame(bytes.fromhex("17C100520000000000D6"))
    down = parse_frame(bytes.fromhex("00C100510000000000EE"))

    assert up.usage != down.usage
    assert (up.label, down.label) == ("Up Arrow", "Down Arrow")


def test_the_same_button_decodes_the_same_from_either_address():
    """Discovery and session copies of one press must agree on the button."""
    first = parse_frame(bytes.fromhex("17C3E90000000000003D"))
    later = parse_frame(bytes.fromhex("00C3E900000000000054"))

    assert first.first_packet and not later.first_packet
    assert first.usage == later.usage == 0x00E9
    assert first.signature == later.signature


def test_status_frames_are_not_buttons():
    """0x4F reports repeat and vary like button traffic but carry no button."""
    for raw in ("174F0700000000000093", "004F03000000000000AE", "004F00044C0000000061"):
        frame = parse_frame(bytes.fromhex(raw))
        assert frame.is_button is False
        assert frame.usage is None


def test_release_reports_are_flagged_but_still_typed():
    keyboard = parse_frame(bytes.fromhex("00C1000000000000003F"))
    consumer = parse_frame(bytes.fromhex("00C3000000000000003D"))

    assert (keyboard.is_release, consumer.is_release) == (True, True)
    assert (keyboard.kind, consumer.kind) == ("keyboard", "consumer")
    assert keyboard.is_button and consumer.is_button


def test_consumer_usage_is_little_endian():
    """E9 00 is usage 0x00E9, not 0xE900 -- a byte-order slip would name the wrong key."""
    assert parse_frame(bytes.fromhex("17C3E90000000000003D")).usage == 0x00E9


def test_unknown_report_id_still_parses():
    """An unrecognised report must not be dropped; it is evidence to look at later."""
    payload = bytes([0x00, 0x99, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x66])
    frame = parse_frame(payload)

    assert frame is not None
    assert frame.kind == "unknown"
    assert frame.signature == "99010000"


def test_bad_checksum_is_rejected():
    assert validate_checksum(b"") is False
    assert parse_frame(bytes.fromhex("17C100520000000000FF")) is None


def test_unknown_length_is_rejected():
    # 4 bytes, sums to zero, but no Harmony frame is this long.
    assert parse_frame(bytes([0x01, 0x02, 0x03, 0xFA])) is None


def test_addresses_differ_only_in_the_lsb():
    assert discovery_address(NETWORK_ADDRESS) == bytes.fromhex("00129BFCB6")
    assert session_address(NETWORK_ADDRESS) == NETWORK_ADDRESS
    assert discovery_address(NETWORK_ADDRESS)[1:] == session_address(NETWORK_ADDRESS)[1:]
