"""harmony_receiver: capture and decode Logitech Harmony remote RF24 traffic.

Typical usage as a library, when the network address isn't known yet::

    from harmony_receiver import create_radio, discover_network_address, HarmonyReceiver

    radio = create_radio()  # put the Hub into pairing mode first
    address, channel = discover_network_address(radio)
    for event in HarmonyReceiver(radio, address).events(start_channel=channel):
        print(event)

Or, once the address is already known, with learned button names attached::

    from harmony_receiver import ButtonMap, create_radio, HarmonyReceiver

    buttons = ButtonMap.load("buttons.json")
    radio = create_radio()
    receiver = HarmonyReceiver(radio, bytes.fromhex("17129BFCB6"), resolve_label=buttons.resolve)
    for event in receiver.events():
        print(event.kind, event.name)

Other code can subscribe to the same events instead of driving the loop::

    receiver.subscribe(lambda event: publish_to_mqtt(event))
    for event in receiver.events():  # still needed to actually run the loop
        pass

The `protocol`, `events`, `tracking`, `profiles`, and `capture` submodules have
no hardware dependencies and can be imported and unit tested without an
nRF24L01+ radio attached.
"""

from __future__ import annotations

from .capture import CaptureLog
from .dispatcher import EventDispatcher
from .events import RemoteEvent
from .pairing import PairingCancelled, PairingTimeout, discover_network_address, sniff_network_address
from .profiles import ButtonMap, ButtonProfile
from .hid import consumer_name, keyboard_name
from .protocol import (
    HARMONY_CHANNELS,
    Frame,
    discovery_address,
    parse_frame,
    session_address,
    validate_checksum,
)
from .radio import create_radio, set_silent, set_transceiver
from .receiver import HarmonyReceiver
from .tracking import PressTracker

__version__ = "0.2.0"

__all__ = [
    "ButtonMap",
    "ButtonProfile",
    "CaptureLog",
    "EventDispatcher",
    "Frame",
    "HARMONY_CHANNELS",
    "HarmonyReceiver",
    "PairingCancelled",
    "PairingTimeout",
    "PressTracker",
    "RemoteEvent",
    "consumer_name",
    "keyboard_name",
    "create_radio",
    "discover_network_address",
    "discovery_address",
    "parse_frame",
    "session_address",
    "set_silent",
    "set_transceiver",
    "sniff_network_address",
    "validate_checksum",
]
