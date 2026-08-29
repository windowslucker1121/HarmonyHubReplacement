"""Recovers a Harmony remote's RF24 network address, from its Hub or without one."""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any, Callable, Optional

from .framing import PREAMBLES, find_addresses
from .protocol import HARMONY_CHANNELS, PAIRING_ADDRESS, PAIR_MESSAGE, PING_MESSAGE
from .radio import reset_radio, set_promiscuous

logger = logging.getLogger(__name__)


class PairingTimeout(TimeoutError):
    """No Hub acknowledged the pairing handshake within the timeout."""


class PairingCancelled(RuntimeError):
    """The caller asked to stop before a Hub answered."""


def discover_network_address(
    radio: Any,
    timeout_sec: float = 60,
    should_stop: Optional[Callable[[], bool]] = None,
) -> tuple[bytes, int]:
    """Actively transmits the Harmony pairing handshake until a Hub responds.

    Put the Hub into pairing mode (its pair/reset button) before calling
    this. Transmits on the shared pairing address, hopping across all 12
    Harmony channels until a Hub ACKs, then keeps pinging until the Hub's
    ACK payload reveals the remote's assigned network address.

    Returns the 5-byte network address and the channel the Hub was found on.
    Raises PairingTimeout if no Hub responds within `timeout_sec`.

    `should_stop` is polled once per iteration and raises `PairingCancelled`
    when it returns true. This runs for up to a minute with no natural
    cancellation point, so a caller driving it from a UI needs some way to
    give up that does not involve abandoning a thread still holding the radio.
    """
    radio.open_tx_pipe(PAIRING_ADDRESS)
    radio.listen = False

    channel_idx = 0
    ping_retries = 0
    start_time = time.monotonic()

    while time.monotonic() - start_time < timeout_sec:
        if should_stop is not None and should_stop():
            raise PairingCancelled("Pairing was cancelled before a Hub answered.")

        if ping_retries == 0:
            radio.channel = HARMONY_CHANNELS[channel_idx]
            result = radio.send(PAIR_MESSAGE)
            if result:
                ping_retries = 10
            else:
                channel_idx = (channel_idx + 1) % len(HARMONY_CHANNELS)
        else:
            result = radio.send(PING_MESSAGE)
            ping_retries -= 1

        if isinstance(result, (bytes, bytearray)) and len(result) == 22:
            # payload[3:8] arrives MSB-first; our driver's address byte order
            # is LSB-first (see protocol.PAIRING_ADDRESS), so reverse it after
            # applying the Hub's LSB correction.
            address = bytearray(result[3:8])
            address[4] = (address[4] - 1) & 0xFF
            address = bytes(reversed(address))

            # This query-only handshake can leave the Hub's internal pairing
            # counter drifted from whatever the already-paired remote is
            # still actually using, by an amount that isn't reliably
            # constant (see github.com/joakimjalden/Harmoino issue #3). The
            # LSB here may therefore be off by a small margin; HarmonyReceiver
            # self-corrects it at runtime from real traffic on the masked
            # pipe, so treat this as a good starting point rather than exact.
            channel = HARMONY_CHANNELS[channel_idx]
            logger.info("Captured network address %s on channel %d", address.hex().upper(), channel)
            return address, channel

        time.sleep(0.1)

    raise PairingTimeout("Pairing acquisition timed out without receiving a valid frame.")


# -- Hub-less discovery -------------------------------------------------------
#
# `discover_network_address` above needs a real Harmony Hub in pairing mode to
# answer. This does not: it puts the radio into promiscuous mode (see
# `radio.set_promiscuous`) and recovers the remote's own address straight out
# of its ordinary transmissions, the way LeoKlaus/Equilibrium's
# `discover_remote_address.py` first did for a Harmony remote. Slower and
# less certain than the Hub handshake -- it depends on catching a real
# transmission mid-sweep rather than a Hub answering on request -- so it is
# offered as a second option, not a replacement.

#: How many separate sightings of the same shared address bytes are needed
#: before trusting them. One CRC-16 pass over noise is unlikely but not
#: negligible over a long sweep; two independent sightings is what
#: Equilibrium settled on and this reuses that figure.
_CONFIRMATIONS_NEEDED = 2

#: Time spent on each (channel, preamble) combination while sweeping. Short,
#: so a full 24-combination sweep (12 channels x 2 preambles) comes back
#: around every few seconds -- long searches are made of many quick sweeps,
#: not one slow one, so a button pressed at any point during the search has
#: many chances to land while the radio is listening on the right combination.
_COMBO_DWELL = 0.5

#: How long the final confirmation step gets to see real matched traffic
#: before giving up on an otherwise-promising candidate.
_VERIFY_TIMEOUT = 20.0


def sniff_network_address(
    radio: Any,
    timeout_sec: float = 90,
    verify_timeout_sec: float = _VERIFY_TIMEOUT,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[bytes, int]:
    """Recovers the network address by listening to the remote directly -- no Hub needed.

    Two phases. First, a promiscuous sweep across every channel and both
    preamble polarities (which one actually catches this unit's traffic is
    not knowable in advance) recovers full addresses out of raw noise via
    `framing.find_addresses`. An address is trusted once its shared 4 bytes
    have shown up `_CONFIRMATIONS_NEEDED` times -- the assigned first byte
    can vary between sightings depending on what promiscuous mode happened
    to catch, so whichever sighting had a non-zero one is kept in preference
    to a placeholder zero, but a confirmed-zero candidate is still usable:
    `HarmonyReceiver._resync_from_first_packet` corrects it from the first
    real button press either way, which is exactly what phase two waits for.

    Second, the candidate is proven against real matched-address traffic
    using a temporary `HarmonyReceiver` -- the same receive path a live hub
    uses, not a separate check -- so success here means the address is
    already known to work, not just well-attested.

    Returns the resynced 5-byte network address and the channel real traffic
    was confirmed on, exactly like `discover_network_address`.

    Raises `PairingTimeout` if no address is confirmed within `timeout_sec`
    (phase one) or `verify_timeout_sec` (phase two -- its own budget, not
    carved out of `timeout_sec`, since a candidate worth verifying at all
    has already used however long phase one took). `should_stop` is polled
    throughout both phases, same contract as `discover_network_address`.
    """
    def _check_stop(message: str) -> None:
        if should_stop is not None and should_stop():
            raise PairingCancelled(message)

    def _report(message: str) -> None:
        logger.info(message)
        if on_progress is not None:
            on_progress(message)

    _report("Listening for your remote -- press and release buttons on it repeatedly.")

    shared_counts: "Counter[bytes]" = Counter()
    best_full: "dict[bytes, bytes]" = {}
    confirmed_shared: Optional[bytes] = None

    combos = [(channel, preamble) for channel in HARMONY_CHANNELS for preamble in PREAMBLES]
    combo_index = 0
    start_time = time.monotonic()

    while confirmed_shared is None:
        _check_stop("Pairing was cancelled before an address was recovered.")
        if time.monotonic() - start_time > timeout_sec:
            raise PairingTimeout("No remote traffic was recovered before the timeout.")

        channel, preamble = combos[combo_index % len(combos)]
        combo_index += 1
        set_promiscuous(radio, preamble)
        radio.channel = channel
        radio.listen = True

        combo_deadline = time.monotonic() + _COMBO_DWELL
        while time.monotonic() < combo_deadline:
            _check_stop("Pairing was cancelled before an address was recovered.")
            if not radio.available():
                time.sleep(0.001)
                continue
            payload = radio.read(32)
            if not payload:
                continue
            for address in find_addresses(bytes(payload)):
                shared = address[1:]
                shared_counts[shared] += 1
                if address[0] != 0x00 or shared not in best_full:
                    best_full[shared] = address
                if shared_counts[shared] >= _CONFIRMATIONS_NEEDED:
                    confirmed_shared = shared
            if confirmed_shared is not None:
                break

    candidate = best_full[confirmed_shared]
    _report(f"Found a candidate address {candidate.hex().upper()} -- confirming it...")

    reset_radio(radio)
    return _verify_candidate(radio, candidate, verify_timeout_sec, should_stop, _report)


def _verify_candidate(
    radio: Any,
    candidate: bytes,
    verify_timeout_sec: float,
    should_stop: Optional[Callable[[], bool]],
    report: Callable[[str], None],
) -> tuple[bytes, int]:
    """Proves a candidate address against real traffic, via a throwaway HarmonyReceiver.

    A local import: `receiver.py` has no reason to import from `pairing.py`,
    so importing `HarmonyReceiver` at module load time here would only
    invite a cycle for no benefit -- this is the one function that needs it.
    """
    from .receiver import HarmonyReceiver

    report("Confirming the address -- press a button on the remote again.")
    deadline = time.monotonic() + verify_timeout_sec

    def _stop_or_expired() -> bool:
        return (should_stop is not None and should_stop()) or time.monotonic() > deadline

    receiver = HarmonyReceiver(radio, candidate)
    for _frame in receiver.frames(probe_interval=0, should_stop=_stop_or_expired):
        if receiver.locked_channel is not None:
            report(f"Confirmed on channel {receiver.locked_channel}.")
            return receiver.network_address, receiver.locked_channel

    if should_stop is not None and should_stop():
        raise PairingCancelled("Pairing was cancelled before the address was confirmed.")
    raise PairingTimeout(
        f"Found candidate address {candidate.hex().upper()} but couldn't confirm it against real traffic."
    )
