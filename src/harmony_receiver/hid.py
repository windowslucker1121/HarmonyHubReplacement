"""HID usage tables for naming Harmony button reports.

The remote does not invent its own button codes: it sends standard USB HID
usages, so a button's identity can be *read* rather than learned. Captured
evidence, each confirmed against the button name the operator reported
pressing at the time:

    C1 00 52 00  ->  keyboard usage 0x52  ->  Up Arrow      ("Move Cursor Up")
    C1 00 51 00  ->  keyboard usage 0x51  ->  Down Arrow    ("Move Cursor Down")
    C3 E9 00 00  ->  consumer usage 0x0E9 ->  Volume Up     ("Volume Up")

Only the usages a remote control plausibly emits are listed. Anything absent
still decodes -- it just reports as an unnamed usage number, which is enough
to learn a label for by hand.
"""

from __future__ import annotations

from typing import Dict, Optional

# HID Usage Page 0x07 (Keyboard/Keypad).
KEYBOARD_USAGES: Dict[int, str] = {
    0x28: "Enter",
    0x54: "Keypad /",
    0x55: "Keypad *",
    0x56: "Keypad -",
    0x57: "Keypad +",
    0x65: "Application/Menu Key",
    0x29: "Escape",
    0x2A: "Backspace",
    0x2B: "Tab",
    0x2C: "Space",
    0x4F: "Right Arrow",
    0x50: "Left Arrow",
    0x51: "Down Arrow",
    0x52: "Up Arrow",
    0x53: "Num Lock",
    0x58: "Keypad Enter",
    **{0x04 + i: chr(ord("A") + i) for i in range(26)},
    **{0x1E + i: str((i + 1) % 10) for i in range(10)},
    **{0x3A + i: f"F{i + 1}" for i in range(12)},
}

# HID Usage Page 0x0C (Consumer), which is where a remote's media keys live.
# Values follow the USB HID Usage Tables; the Media Select block in
# particular is easy to misremember, so it is transcribed in full order.
CONSUMER_USAGES: Dict[int, str] = {
    0x0030: "Power",
    0x0031: "Reset",
    0x0032: "Sleep",
    0x0040: "Menu",
    0x0041: "Menu Pick",
    0x0042: "Menu Up",
    0x0043: "Menu Down",
    0x0044: "Menu Left",
    0x0045: "Menu Right",
    0x0046: "Menu Escape",
    0x0047: "Menu Value Increase",
    0x0048: "Menu Value Decrease",
    0x0060: "Data On Screen",
    0x0061: "Closed Caption",
    0x0062: "Closed Caption Select",
    0x0063: "VCR/TV",
    0x0064: "Broadcast Mode",
    0x0065: "Snapshot",
    0x0066: "Still",
    0x006F: "Brightness Increment",
    0x0070: "Brightness Decrement",
    0x0082: "Mode Step",
    0x0083: "Recall Last",
    0x0084: "Enter Channel",
    0x0086: "Channel",
    0x0088: "Media Select Computer",
    0x0089: "Media Select TV",
    0x008A: "Media Select WWW",
    0x008B: "Media Select DVD",
    0x008C: "Media Select Telephone",
    0x008D: "Program Guide",
    0x008E: "Media Select Video Phone",
    0x008F: "Media Select Games",
    0x0090: "Media Select Messages",
    0x0091: "Media Select CD",
    0x0092: "Media Select VCR",
    0x0093: "Media Select Tuner",
    0x0094: "Quit",
    0x0095: "Help",
    0x0096: "Media Select Tape",
    0x0097: "Media Select Cable",
    0x0098: "Media Select Satellite",
    0x0099: "Media Select Security",
    0x009A: "Media Select Home",
    0x009B: "Media Select Call",
    0x009C: "Channel Up",
    0x009D: "Channel Down",
    0x009E: "Media Select SAP",
    0x00A0: "VCR Plus",
    0x00B0: "Play",
    0x00B1: "Pause",
    0x00B2: "Record",
    0x00B3: "Fast Forward",
    0x00B4: "Rewind",
    0x00B5: "Next Track",
    0x00B6: "Previous Track",
    0x00B7: "Stop",
    0x00B8: "Eject",
    0x00B9: "Random Play",
    0x00BC: "Repeat",
    0x00CD: "Play/Pause",
    0x00E2: "Mute",
    0x00E9: "Volume Up",
    0x00EA: "Volume Down",
    0x0183: "AL Consumer Control Config",
    0x018A: "AL Email Reader",
    0x0192: "AL Calculator",
    0x0194: "AL Local Machine Browser",
    0x0196: "AL Internet Browser",
    0x0221: "AC Search",
    0x0223: "AC Home",
    0x0224: "AC Back",
    0x0225: "AC Forward",
    0x0226: "AC Stop",
    0x0227: "AC Refresh",
    0x022A: "AC Bookmarks",
}

# Usages this remote emits that are outside the standard tables: Logitech
# uses them for hardware that HID has no name for -- the activity buttons
# and the coloured keys. They decode fine, they just cannot be named from a
# spec, so `profiles` is where a human puts the real labels.
VENDOR_RANGES = ((0x01E0, 0x01FF), (0x0FF0, 0x0FFF))


def is_vendor_usage(usage: int) -> bool:
    """Whether a consumer usage falls in one of this remote's vendor-specific blocks."""
    return any(low <= usage <= high for low, high in VENDOR_RANGES)


# HID Usage Page 0x01 (Generic Desktop), System Control subset.
SYSTEM_USAGES: Dict[int, str] = {
    0x81: "System Power Down",
    0x82: "System Sleep",
    0x83: "System Wake Up",
}


def keyboard_name(usage: int) -> Optional[str]:
    """The name of a HID keyboard usage, or None if it isn't a listed one."""
    return KEYBOARD_USAGES.get(usage)


def consumer_name(usage: int) -> Optional[str]:
    """The name of a HID consumer-control usage, or None if it isn't a listed one."""
    return CONSUMER_USAGES.get(usage)
