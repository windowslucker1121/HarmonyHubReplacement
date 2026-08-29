"""Receive-only sniffing loop that turns raw nRF24 packets into Harmony events."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterator, Optional

from .capture import CaptureLog
from .dispatcher import EventDispatcher, Subscriber
from .events import RemoteEvent
from .protocol import (
    DISCOVERY_PIPE,
    HARMONY_CHANNELS,
    PING_MESSAGE,
    SESSION_PIPE,
    Frame,
    discovery_address,
    parse_frame,
    session_address,
)
from .radio import set_silent, set_transceiver
from .tracking import PressTracker

logger = logging.getLogger("RECEIVER")

DEFAULT_DWELL = 0.03  # seconds spent listening on each channel while searching

# How often the locked loop surfaces "nothing arrived" so the release timer
# can run. Short enough that a release lands promptly, long enough that the
# radio isn't re-polled pointlessly between packets 100ms apart.
IDLE_POLL_WINDOW = 0.05

# How long the air must stay quiet before the loop spends a probe on
# re-locating the Hub. This Hub was measured changing channel on its own
# every 10-25s, so a channel found once goes stale well within a session.
DEFAULT_PROBE_INTERVAL = 4.0

# Auto-retransmit count used while probing. Eleven of the twelve channels
# have nobody on them, and a failed send costs one retry slot each, so
# keeping this low is what makes a full sweep cheap enough to run in-loop.
PROBE_RETRIES = 2


class HarmonyReceiver:
    """Listens for button events from a Harmony remote already bound to a Hub.

    Use `pairing.discover_network_address()` first if `network_address` isn't
    already known.

    The radio never answers the remote: with the real Hub powered on, an ACK
    from us would collide with the Hub's (see `radio.set_silent`). It does
    transmit pings of its own, because the Hub moves between channels during
    a session and has to be re-found, but only while the air is quiet -- see
    `sniff()` for how those two requirements are reconciled.

    Events are available two ways, and both work at once: iterate `events()`
    directly, and/or `subscribe()` a callback to be notified as the same
    events are produced -- the latter doesn't require being the one driving
    the loop, so other code can listen in independently.
    """

    def __init__(
        self,
        radio: Any,
        network_address: bytes,
        capture: Optional[CaptureLog] = None,
        resolve_label: Optional[Callable[[str], Optional[str]]] = None,
    ):
        self.radio = radio
        self.network_address = bytes(network_address)
        self.capture = capture
        self._dispatcher = EventDispatcher()
        self._tracker = PressTracker(resolve_label=resolve_label)
        self.locked_channel: Optional[int] = None

    def subscribe(self, callback: Subscriber) -> None:
        self._dispatcher.subscribe(callback)

    def unsubscribe(self, callback: Subscriber) -> None:
        self._dispatcher.unsubscribe(callback)

    def _open_pipes(self) -> None:
        # Order matters: pipes 2-5 store only their own address LSB and
        # inherit the upper four bytes from pipe 1, so the discovery address
        # has to be written first for the session address to land correctly.
        self.radio.open_rx_pipe(DISCOVERY_PIPE, discovery_address(self.network_address))
        self.radio.open_rx_pipe(SESSION_PIPE, session_address(self.network_address))
        self.radio.listen = True

    def _fast_probe(self) -> Optional[int]:
        """One quick sweep for the Hub's current channel, as cheaply as possible.

        Kept deliberately short because it runs inside the capture loop: the
        radio is deaf while transmitting, and `radio.send()` flushes the RX
        FIFO. Cutting the auto-retransmit count makes a *failed* send return
        almost immediately, which is the common case -- eleven of the twelve
        channels have nobody on them.

        Answers only when exactly one channel replies. Two repliers means
        something is off and a guess would just park us on the wrong one.
        """
        previous_arc = self.radio.arc
        self.radio.arc = PROBE_RETRIES
        self.radio.open_tx_pipe(session_address(self.network_address))
        self.radio.listen = False
        try:
            answering = []
            for channel in HARMONY_CHANNELS:
                self.radio.channel = channel
                if self.radio.send(PING_MESSAGE):
                    answering.append(channel)
        finally:
            self.radio.arc = previous_arc

        return answering[0] if len(answering) == 1 else None

    def scan_energy(self, duration: float = 20.0) -> "dict[int, int]":
        """Counts how often each channel carries RF energy, using the radio's RPD.

        The Received Power Detector trips on any signal above roughly -64 dBm
        and knows nothing about addresses, CRC, or packet format. That makes
        it the tool of last resort when packet capture returns nothing: it
        separates "the remote is not transmitting" from "we are not decoding
        it", which no amount of staring at an empty FIFO can do. It is how
        this remote's actual transmit channels were found after packet
        capture had failed on every other theory.

        Returns a count per channel, alongside the number of sweeps taken.
        """
        set_silent(self.radio)
        self.radio.listen = True

        hits = {channel: 0 for channel in HARMONY_CHANNELS}
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            for channel in HARMONY_CHANNELS:
                # RPD latches high until the radio leaves RX, so drop out of
                # RX between samples or every reading after the first is stale.
                self.radio.listen = False
                self.radio.channel = channel
                self.radio.listen = True
                if self.radio.rpd:
                    hits[channel] += 1
        return hits

    def scan_channels(self, rounds: int = 5) -> "dict[int, int]":
        """Pings every channel `rounds` times and counts the hardware ACKs each returns.

        Sweeping all 12 every round, rather than stopping at the first ACK,
        is what makes the result interpretable. A single channel answering
        consistently is the Hub. *Every* channel answering means the ACK
        detection itself is lying and no probe result can be trusted --
        exactly the failure that a stop-at-first-ACK probe reports as a
        confident (and wrong) channel.

        This transmits, so it is a deliberate diagnostic, never part of the
        capture loop: `radio.send()` flushes the RX FIFO on every call, which
        silently destroys button packets waiting to be read.
        """
        set_transceiver(self.radio)
        self.radio.open_tx_pipe(session_address(self.network_address))
        self.radio.listen = False

        acks = {channel: 0 for channel in HARMONY_CHANNELS}
        for _ in range(rounds):
            for channel in HARMONY_CHANNELS:
                self.radio.channel = channel
                if self.radio.send(PING_MESSAGE):
                    acks[channel] += 1
        return acks

    def probe_for_channel(self, rounds: int = 5) -> Optional[int]:
        """Locates the Hub's channel by scanning, or None if the scan is inconclusive.

        Returns a channel only when it out-answers every other one; if all of
        them reply, or none does, the probe admits it doesn't know instead of
        handing back a channel the caller would sit on deafly.
        """
        acks = self.scan_channels(rounds)
        answering = {channel: count for channel, count in acks.items() if count}

        if not answering:
            logger.info("No channel answered the probe.")
            return None
        if len(answering) == len(HARMONY_CHANNELS):
            logger.warning("Every channel answered the probe -- ACK detection is unreliable here, ignoring it.")
            return None

        best = max(answering, key=lambda channel: answering[channel])
        logger.info("Probe favours channel %d (%d/%d ACKs; %s)", best, acks[best], rounds, answering)
        return best

    def _resync_from_first_packet(self, frame: Frame) -> None:
        """Corrects the session address LSB from a first packet that carries it.

        The discovery address zeroes the LSB, so pipe 1 matches on the upper
        four bytes alone -- meaning button traffic is captured even when the
        LSB we were given is wrong. Every first packet announces the true LSB
        as its own byte 0, and the Hub's pairing counter is known to drift
        between a query-only handshake and what the remote actually uses (see
        pairing.py), so trust real traffic over the handshake's guess.
        """
        observed_lsb = frame.payload[0]
        if observed_lsb == self.network_address[0]:
            return

        self.network_address = bytes([observed_lsb]) + self.network_address[1:]
        self.radio.open_rx_pipe(SESSION_PIPE, session_address(self.network_address))
        logger.info(
            "Resynced network address to %s (first packet announced LSB 0x%02X)",
            self.network_address.hex().upper(), observed_lsb,
        )
        if self.capture is not None:
            self.capture.mark("address_resync", address=self.network_address.hex().upper())

    def _drain(self, channel: int) -> Iterator[Frame]:
        """Reads every packet waiting in the RX FIFO (it holds up to three)."""
        while self.radio.available():
            # `.pipe` must be read before `.read()` -- reading the FIFO clears
            # the status byte, so `.pipe` would report empty afterwards.
            pipe = self.radio.pipe
            payload = self.radio.read()
            if not payload:
                break

            payload = bytes(payload)
            frame = parse_frame(payload, pipe=pipe, channel=channel)
            if self.capture is not None:
                self.capture.packet(payload, pipe, channel, frame.kind if frame else None)

            logger.debug(
                "channel=%2d pipe=%s len=%2d kind=%-8s raw=%s",
                channel, pipe, len(payload), frame.kind if frame else "invalid", payload.hex().upper(),
            )
            if frame is not None:
                if frame.first_packet:
                    self._resync_from_first_packet(frame)
                yield frame

    def sniff(
        self,
        start_channel: Optional[int] = None,
        dwell: float = DEFAULT_DWELL,
        probe_interval: float = DEFAULT_PROBE_INTERVAL,
        silent: bool = True,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Iterator[Optional[Frame]]:
        """Yields raw Harmony frames forever, receive-only.

        Yields None whenever a poll window elapses with nothing received. A
        release is only ever visible as traffic *stopping*, so the consumer
        has to be handed the passage of time as well as the packets --
        otherwise a remote that falls silent mid-press would stay stuck
        "held" until something unrelated arrived. `frames()` filters those
        Nones out; `events()` uses them to run the release timer.

        Follows the Hub rather than locking onto it. Measured against this
        Hub over a minute, exactly one channel answers at any moment, but
        *which* one changes on its own every ten to twenty-five seconds with
        nobody touching the remote. A channel found once therefore goes stale
        within a session, which is why this re-probes on a timer instead of
        trusting a lock -- an earlier version held its first lock forever and
        spent whole sessions tuned to a channel the Hub had already left.

        Probing costs a transmission, and the radio is deaf while it sends;
        worse, `radio.send()` flushes the RX FIFO, so a badly-timed probe
        eats packets that arrived but hadn't been read yet. Two things keep
        that harmless: `_fast_probe` cuts the retry count so a full sweep
        takes tens of milliseconds, and a probe only ever runs after
        `probe_interval` seconds of *silence* -- while packets are actually
        flowing, the loop stays quiet and just listens.

        With no usable probe result it falls back to sweeping the 12
        candidates passively. That is worth more than it sounds: on the first
        press after going idle the remote itself tries every channel until
        the Hub answers, so its identifying packets go out on whichever one
        we happen to be sitting on.

        `silent=False` leaves auto-ACK enabled on the Harmony pipes, which
        makes this radio answer the remote and collide with the real Hub's
        ACK. It is only here to A/B against the silent path.

        `should_stop` is polled once per outer iteration and once per dwell
        window, so a caller running this on a worker thread has somewhere to
        ask it to return -- this loop has no other cancellation point, and
        the SPI transactions inside `_drain` are not safe to abandon
        mid-call, so tearing down the radio out from under a still-running
        thread is what leaves it wedged rather than merely stopped.
        """
        set_silent(self.radio) if silent else set_transceiver(self.radio)
        self._open_pipes()

        self.locked_channel = start_channel
        tuned: Optional[int] = None
        last_traffic = time.monotonic()
        # A given channel is trusted for a full interval before being
        # re-checked; without one, probe immediately rather than sweeping
        # blindly when a single cheap sweep can just answer the question.
        last_probe = time.monotonic() if start_channel is not None else -max(probe_interval, 1.0)

        if start_channel is not None:
            logger.info("Starting on channel %d", start_channel)

        while True:
            if should_stop is not None and should_stop():
                return

            now = time.monotonic()
            if probe_interval > 0 and now - last_traffic > probe_interval and now - last_probe > probe_interval:
                probed = self._fast_probe()
                self._open_pipes()  # probing leaves the radio in TX; always return to RX
                tuned = self.locked_channel
                last_probe = time.monotonic()
                if probed is not None and probed != self.locked_channel:
                    logger.info("Hub is on channel %d now", probed)
                    self.locked_channel = probed
                    if self.capture is not None:
                        self.capture.mark("channel_change", channel=probed)

            locked = self.locked_channel
            # Nothing found: sweep, giving each channel a short slice so the
            # sweep comes back around quickly. Following a known channel, the
            # window is just how often the release timer gets to run.
            candidates = (locked,) if locked is not None else tuple(HARMONY_CHANNELS)
            window = IDLE_POLL_WINDOW if locked is not None else dwell

            for channel in candidates:
                if channel != tuned:
                    self.radio.channel = channel
                    tuned = channel

                received = False
                deadline = time.monotonic() + window
                while time.monotonic() < deadline:
                    if should_stop is not None and should_stop():
                        return
                    for frame in self._drain(channel):
                        received = True
                        if self.locked_channel != channel:
                            self.locked_channel = channel
                            logger.info("Found real traffic on channel %d", channel)
                            if self.capture is not None:
                                self.capture.mark("channel_lock", channel=channel, source="traffic")
                        yield frame

                if received:
                    last_traffic = time.monotonic()
                else:
                    yield None
                if locked is None and self.locked_channel is not None:
                    break  # restart the outer loop, now following that channel

    def frames(self, **kwargs: Any) -> Iterator[Frame]:
        """Yields raw Harmony frames forever, receive-only.

        `kwargs` are passed through to `sniff()`.
        """
        for frame in self.sniff(**kwargs):
            if frame is not None:
                yield frame

    def events(self, **kwargs: Any) -> Iterator[RemoteEvent]:
        """Yields press / repeat / release events forever.

        `kwargs` are passed through to `sniff()`.
        """
        for frame in self.sniff(**kwargs):
            if frame is not None:
                for event in self._tracker.feed(frame):
                    self._dispatcher.publish(event)
                    yield event
            for event in self._tracker.tick():
                self._dispatcher.publish(event)
                yield event
