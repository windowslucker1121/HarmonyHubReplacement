"""FT232H / Raspberry Pi + nRF24L01+ hardware bring-up.

This is the only module in the package that touches board-specific hardware.
Blinka itself decides which: on Linux it auto-detects a native board (a
Raspberry Pi's own SPI/GPIO) from `/proc/cpuinfo`, so nothing here needs to
change to move from an FT232H breakout on a dev PC to a Pi's header pins --
only `csn_pin`/`ce_pin` do, since the two boards use different pin names.
"""

from __future__ import annotations

import logging
import os
import platform
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CSN_PIN = "C0"
DEFAULT_CE_PIN = "D4"

#: Where `create_radio` records the pins it claimed, so `release_radio` can
#: hand them back. An attribute on the radio rather than module state,
#: because two radios on two FT232Hs must not share one entry.
_PINS_ATTR = "_harmony_pins"


def create_radio(csn_pin: str = DEFAULT_CSN_PIN, ce_pin: str = DEFAULT_CE_PIN) -> Any:
    """Brings up an nRF24L01+ radio configured for the Harmony protocol.

    On Windows or macOS this is an FT232H breakout: wire the nRF24L01+'s
    SCK/MOSI/MISO to the FT232H's hardware SPI pins (D0/D1/D2) and CSN/CE to
    `csn_pin`/`ce_pin` (default C0/D4 -- D0-D3 are reserved for hardware SPI
    and cannot be used here).

    On Linux -- a Raspberry Pi -- Blinka detects the board itself and talks
    to its native SPI0 (SCK/MOSI/MISO on the header's dedicated pins) and
    GPIO, so `csn_pin`/`ce_pin` should instead name two free GPIO pins there,
    e.g. `D5`/`D6`. Forcing the FT232H driver only happens off Linux, so this
    path is untouched; set `BLINKA_FT232H=1` yourself first if a Linux dev
    machine also needs the FT232H bridge instead of its own SPI.

    The radio comes back in transceiver mode. Call `set_silent()` before
    sniffing so it stops answering traffic that isn't addressed to us.
    """
    if "BLINKA_FT232H" not in os.environ and platform.system() != "Linux":
        os.environ["BLINKA_FT232H"] = "1"

    import board
    import digitalio
    from circuitpython_nrf24l01.rf24 import RF24

    csn = digitalio.DigitalInOut(getattr(board, csn_pin))
    ce = digitalio.DigitalInOut(getattr(board, ce_pin))
    spi_bus = board.SPI()

    radio = RF24(spi_bus, csn, ce)
    radio.data_rate = 2  # 2 Mbps air data rate (Harmony protocol)
    radio.crc = 2  # 16-bit CRC
    radio.dynamic_payloads = True
    set_transceiver(radio)
    setattr(radio, _PINS_ATTR, (csn, ce))
    return radio


def release_radio(radio: Any) -> None:
    """Hands the CE/CSN pins back, so the radio can be opened again later.

    `DigitalInOut` claims a pin for the lifetime of the process and raises on
    a second claim. Without this, anything that opens the radio twice -- a
    hub restarted from the settings page, a hardware check run before
    starting -- fails on the second attempt with a "pin in use" error that
    has nothing to do with the radio being absent or misconfigured.

    Safe on a radio that was never registered, and safe to call twice.
    """
    for pin in getattr(radio, _PINS_ATTR, ()):
        try:
            pin.deinit()
        except Exception:  # pragma: no cover - hardware teardown
            logger.debug("Pin would not deinit", exc_info=True)
    if hasattr(radio, _PINS_ATTR):
        delattr(radio, _PINS_ATTR)


def set_transceiver(radio: Any) -> None:
    """Enables auto-ACK and the ACK-payload feature: the radio answers what it hears.

    Required to talk *to* a Hub -- both the pairing handshake and any active
    channel probe rely on a hardware ACK coming back, and the Hub's pairing
    reply arrives as an ACK payload. Only use this while we are legitimately
    one end of the link.
    """
    radio.auto_ack = True
    radio.ack = True


def set_silent(radio: Any) -> None:
    """Listens without ever transmitting, while still decoding the Hub's packets.

    While the real Hub is powered on it is the one that owes the remote an
    ACK. If we also auto-ACK packets sent to the Hub's address, our reply
    collides with the Hub's on the same channel at the same instant: the
    remote sees a corrupt ACK, retransmits, and can decide the Hub has gone
    away and re-sweep channels. Sniffing therefore has to be receive-only.

    The obvious way to get there -- clearing EN_AA outright -- does not work,
    and fails silently. EN_AA == 0x00 drops the whole chip into legacy
    nRF2401 ShockBurst, whose on-air format has no 9-bit packet control
    field. The Harmony remote transmits Enhanced ShockBurst *with* one, so
    every packet decodes misaligned, fails CRC, and is discarded before it
    ever reaches the FIFO: a receiver that is correctly tuned, correctly
    addressed, and completely deaf.

    So auto-ACK stays enabled on pipe 0, purely to keep EN_AA non-zero and
    the chip in Enhanced ShockBurst. Pipe 0 is never opened for RX (see
    `HarmonyReceiver._open_pipes`, and the `listen` setter closes it), so it
    can never match a packet and never answers one. Pipes 1 and 2 -- the ones
    actually carrying Harmony traffic -- have auto-ACK off and stay mute.
    """
    radio.ack = False  # no ACK payloads
    radio.auto_ack = [True, False, False, False, False, False]  # EN_AA = 0x01


def set_promiscuous(radio: Any, preamble: int) -> None:
    """Puts the radio into address-agnostic sniffing mode, for pairing.sniff_network_address.

    `address_length = 2` is illegal per the datasheet -- SETUP_AW's two
    reserved-looking bits are meant to select 3, 4, or 5 byte addresses, and
    0 is not one of the documented choices. In practice the chip accepts it
    and, combined with CRC and auto-ACK both off, stops trying to match a
    specific address at all: it latches onto anything that looks like a
    preamble followed by the two bytes given here, and hands back whatever
    32 bytes follow. That is deliberately not a real Harmony frame -- see
    `framing.find_addresses`, which recovers the genuine address and CRC
    from the resulting noise instead of trusting this match.

    `preamble` is one of `framing.PREAMBLES`; which one actually catches
    anything is unit-specific and not knowable in advance, so a caller
    sweeps both. This is Travis Goodspeed's nRF24 promiscuous-mode finding,
    applied to this project the way LeoKlaus/Equilibrium's
    `discover_remote_address.py` first did for a Harmony remote specifically.

    `address_length` is set before `open_rx_pipe` here only to mirror that
    prior art's own sequence -- what the chip actually matches against is
    whatever `address_length` holds at the moment `listen` goes high, not
    at the moment the pipe address was written, so this order is not known
    to be load-bearing the way the address-length value itself is.
    """
    radio.auto_ack = False
    radio.crc = 0
    radio.dynamic_payloads = False
    radio.payload_length = 32
    radio.address_length = 2
    radio.open_rx_pipe(1, bytes([0x00, preamble]))


def reset_radio(radio: Any) -> None:
    """Restores normal Harmony-matched addressing after `set_promiscuous`.

    Undoes every property that function touched, in the order
    `create_radio` establishes them in -- address length before anything
    that opens a pipe against it, dynamic payloads before auto-ACK, since
    the driver's own dynamic-payload feature bit depends on auto-ACK being
    meaningful. Leaves the radio in transceiver mode; callers that want
    receive-only silence should follow this with `set_silent`.

    Cheaper than tearing down and recreating the radio object, which is the
    alternative should this reordering ever prove insufficient on real
    hardware -- `release_radio`/`create_radio` around the whole operation.
    """
    radio.address_length = 5
    radio.crc = 2
    radio.dynamic_payloads = True
    set_transceiver(radio)
