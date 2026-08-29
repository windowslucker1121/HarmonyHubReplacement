"""Talks to the one IR receiver and the one IR transmitter this install is
wired to, through pigpio's DMA-timed GPIO waveforms.

**Why pigpio.** A KY-022 receiver demodulates the 38kHz carrier in hardware
and hands back a clean mark/space train, but a KY-005 transmitter is a bare
LED with no oscillator at all -- the carrier has to be generated in
software, with microsecond accuracy over a frame lasting tens of
milliseconds. Bit-banging that from Python (`RPi.GPIO`, `lgpio`) cannot hold
that timing under the interpreter's own scheduling jitter. pigpio's
`pigpiod` daemon builds the whole waveform up front and plays it back off
hardware DMA, which is what makes the timing actually land.

**One gateway, not one per device.** There is exactly one receiver and one
transmitter wired to this Pi (`HubSettings.ir_rx_pin` / `ir_tx_pin`), however
many IR *devices* are configured against them -- a living room TV and a
soundbar sharing one blaster is the ordinary case, not an edge case. So this
is a process-wide singleton (`gateway()`), and every `IrBackend` resolves it
per operation rather than holding a connection of its own. That is what lets
`reconfigure()` apply a pin change instantly: it tears down and reopens the
one shared connection, and no backend has anything to rebuild.

**RX is muted during TX.** Both modules sit on the same small board with no
optical separation, so a transmission would otherwise re-enter the receiver
as a phantom capture -- including during "test this code" playback, which
is exactly when a stray capture would be most confusing.

**Never raises out of `configure()`.** A missing `pigpiod`, an unwired pin,
a daemon that is not running yet -- all of these are recorded as a reason
string and left for `health()` to report, not raised. This is what keeps
every IR device's settings editable from a machine with nothing wired at
all: `configure()` runs at hub startup unconditionally, and a hub that
crashed here because a Raspberry Pi's `pigpiod` service was not yet started
would take the whole hub down with it -- the same failure mode
`HubRuntime.start()`'s own docstring already rules out for the radio.

**A failed connection is retried lazily, not left failed forever.**
`configure()` only runs when the hub starts or an IR setting is saved; if
`pigpiod` was not running at that one moment, the gateway used to stay
recorded as unreachable indefinitely -- correctly reporting the problem, but
giving no way to recover from it short of a full hub restart once the
problem was fixed. `capture()` and `transmit()` now retry the connection
themselves first, so starting `pigpiod` on the device takes effect the next
time someone actually tries to learn or send something.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, List, Optional, Sequence

from .normalise import MIN_PULSES

if TYPE_CHECKING:  # pragma: no cover
    from ..settings import HubSettings

logger = logging.getLogger("HUB.ir.gateway")

#: How long with no edge before a capture is considered a complete frame.
#: Real IR protocols never leave this much silence mid-transmission, and
#: even NEC's repeat gap (~108ms) is much longer, so a capture never
#: straddles one.
DEFAULT_GAP_US = 10_000

DEFAULT_CARRIER_HZ = 38_000
DEFAULT_DUTY_CYCLE = 0.33

#: Minimum spacing between transmissions, and the gap used between repeated
#: frames within a single `transmit()` call -- one number serves both,
#: because both exist for the same reason: real IR receivers need silence
#: between frames to reliably tell one from the next.
DEFAULT_GAP_MS = 40.0

#: pigpio's own ceiling on pulses in one wave. A NEC frame, once the carrier
#: is expanded into individual on/off pulses, comes to roughly 2,000; a
#: 200-bit air-conditioner frame can exceed this. Checked before a wave is
#: ever built, and reported rather than silently truncated.
MAX_WAVE_PULSES = 12_000

#: How long to keep the receiver muted after a transmission ends, past the
#: last pulse actually leaving the LED -- covers the demodulator's own
#: settle/AGC recovery time, which outlasts the waveform itself slightly.
MUTE_TAIL_S = 0.005


class IrHardwareError(RuntimeError):
    """The pin, the daemon, or the request itself stops this from working."""


class IrTimeout(IrHardwareError):
    """No frame arrived before the capture timeout elapsed."""


class IrBusy(RuntimeError):
    """Raised rather than starting a second capture the receiver is already doing."""


def connect_pigpio(host: str, port: int) -> Any:
    """Opens a connection to `pigpiod`. Raises rather than swallowing anything.

    Kept as its own function -- rather than `pigpio.pi(...)` written inline
    in `IrGateway.configure` -- purely so a test can substitute a fake here,
    the same shape as `backends.denon.build_client`.
    """
    import pigpio

    pi = pigpio.pi(host, port)
    if not pi.connected:
        pi.stop()
        raise ConnectionError(f"pigpiod not reachable at {host}:{port}")
    return pi


class _FrameRecorder:
    """Accumulates one capture's edge timings, fed from pigpio's own thread.

    `pigpio.callback` invokes its handler on a thread pigpio owns, not the
    asyncio loop's thread, so touching the loop's `Event` or timers directly
    from there would be a race. Every mutation is instead marshalled onto
    the loop via `call_soon_threadsafe`, which is what makes `wait()` safe to
    await from ordinary async code.
    """

    def __init__(self, gap_us: int) -> None:
        self._gap_us = gap_us
        self._edges: List[int] = []
        self._last_tick: Optional[int] = None
        self._loop = asyncio.get_running_loop()
        self._done = asyncio.Event()
        self._timer: Optional[asyncio.TimerHandle] = None

    def on_edge(self, tick: int) -> None:
        """Called from pigpio's callback thread -- hands off immediately."""
        self._loop.call_soon_threadsafe(self._handle_edge, tick)

    def _handle_edge(self, tick: int) -> None:
        if self._last_tick is not None:
            # pigpio ticks wrap at 2**32 microseconds (~71.6 minutes); a
            # capture never runs anywhere near that long, so one modulo
            # covers the wrap correctly regardless of which side of it the
            # two ticks fall on.
            self._edges.append((tick - self._last_tick) % (2**32))
        self._last_tick = tick
        if self._timer is not None:
            self._timer.cancel()
        self._timer = self._loop.call_later(self._gap_us / 1_000_000, self._done.set)

    async def wait(self) -> List[int]:
        await self._done.wait()
        return self._edges


class IrGateway:
    """The one receiver and the one transmitter this install is wired to."""

    def __init__(self) -> None:
        self._pi: Optional[Any] = None
        self._rx_pin: Optional[int] = None
        self._tx_pin: Optional[int] = None
        self._host = "localhost"
        self._port = 8888
        self._callback: Optional[Any] = None
        self._error: str = "not configured"
        self._muted = False
        self._recorder: Optional[_FrameRecorder] = None
        self._send_lock = asyncio.Lock()
        self._last_send_at = 0.0

    # -- lifecycle ----------------------------------------------------

    def configure(
        self,
        rx_pin: Optional[int],
        tx_pin: Optional[int],
        host: str = "localhost",
        port: int = 8888,
    ) -> None:
        """(Re)connects to pigpiod on the given pins. Never raises.

        Safe to call repeatedly -- `HubRuntime.apply_settings` calls this on
        every settings save that touched an IR field, whether or not
        anything about the wiring actually changed -- and safe to call with
        both pins `None`, which simply leaves the gateway idle. Always tears
        down whatever it already held first: `pigpio.callback` claims a GPIO
        for as long as it stays registered, and leaving the old one running
        while a new one opens on the same pin is exactly the "pin in use"
        failure `harmony_receiver.radio.release_radio` exists to rule out
        for the nRF24 -- the same lesson applies here.
        """
        self.shutdown()
        self._rx_pin, self._tx_pin, self._host, self._port = rx_pin, tx_pin, host, port

        if rx_pin is None and tx_pin is None:
            self._error = "no pins configured"
            return

        self._connect_sync()

    def _connect_sync(self) -> None:
        """Attempts one connection to `pigpiod` on `self._rx_pin`/`self._tx_pin`.

        The blocking half of `configure()`, factored out so `_ensure_connected`
        below can retry exactly the same steps later without duplicating them.
        Never raises -- failure is recorded in `self._error` for `health()`,
        same as `configure()` itself promises.
        """
        try:
            self._pi = connect_pigpio(self._host, self._port)
        except Exception as err:
            self._error = str(err)
            self._pi = None
            return

        self._error = ""
        import pigpio

        if self._rx_pin is not None:
            self._pi.set_mode(self._rx_pin, pigpio.INPUT)
            self._pi.set_pull_up_down(self._rx_pin, pigpio.PUD_UP)
            self._callback = self._pi.callback(self._rx_pin, pigpio.EITHER_EDGE, self._on_edge)
        if self._tx_pin is not None:
            self._pi.set_mode(self._tx_pin, pigpio.OUTPUT)
            self._pi.write(self._tx_pin, 0)

    async def _ensure_connected(self) -> None:
        """Retries the `pigpiod` connection if pins are wired but nothing is connected.

        Called first by both `capture()` and `transmit()`. A no-op once a
        connection already exists, so this costs nothing on the ordinary
        path -- it only ever does work right after `pigpiod` was down and has
        since come back, which is exactly the gap `configure()` alone leaves:
        it only runs at hub startup or on a settings save, never in response
        to the daemon's own state changing on its own.
        """
        if self._pi is not None:
            return
        if self._rx_pin is None and self._tx_pin is None:
            return
        await asyncio.to_thread(self._connect_sync)

    def shutdown(self) -> None:
        """Releases whatever `configure` claimed. Safe to call twice, or first."""
        if self._callback is not None:
            try:
                self._callback.cancel()
            except Exception:
                logger.debug("IR receive callback would not cancel", exc_info=True)
            self._callback = None
        if self._pi is not None:
            try:
                self._pi.wave_clear()
            except Exception:
                logger.debug("Clearing IR waves failed", exc_info=True)
            try:
                self._pi.stop()
            except Exception:
                logger.debug("Closing the pigpio connection failed", exc_info=True)
            self._pi = None

    def health(self) -> "tuple[bool, str]":
        """Whether the gateway is usable right now, and why if it is not."""
        if self._rx_pin is None and self._tx_pin is None:
            return True, "no pins configured"
        if self._pi is None:
            return False, self._error or "pigpiod not reachable"
        wiring = ", ".join(
            part
            for part in (
                f"receive GPIO{self._rx_pin}" if self._rx_pin is not None else None,
                f"transmit GPIO{self._tx_pin}" if self._tx_pin is not None else None,
            )
            if part
        )
        return True, wiring

    @property
    def rx_ready(self) -> bool:
        return self._pi is not None and self._rx_pin is not None

    @property
    def tx_ready(self) -> bool:
        return self._pi is not None and self._tx_pin is not None

    @property
    def rx_configured(self) -> bool:
        """Whether a receive pin is *set*, independent of whether pigpiod is
        currently connected -- unlike `rx_ready`. Distinguishing the two is
        what lets a caller give an instant, precise "no pin set" message
        without misreporting a pin that is set but whose connection has not
        (yet, or again) been established the same way."""
        return self._rx_pin is not None

    @property
    def tx_configured(self) -> bool:
        return self._tx_pin is not None

    # -- capture ----------------------------------------------------------

    def _on_edge(self, gpio: int, level: int, tick: int) -> None:
        # Runs on pigpio's own thread. Kept to nothing but a forwarding call
        # -- see `_FrameRecorder`'s docstring for why.
        if self._muted:
            return
        recorder = self._recorder
        if recorder is not None:
            recorder.on_edge(tick)

    async def capture(self, timeout: float, gap_us: int = DEFAULT_GAP_US) -> List[int]:
        """Waits for one complete IR frame and returns its mark/space durations.

        The result starts on a mark: the receiver idles high, and the first
        edge it ever reports is the carrier switching on. Rejects anything
        with fewer than `normalise.MIN_PULSES` pulses -- a TSOP demodulator
        free-runs under fluorescent light and direct sun, and without that
        floor a stretch of ordinary noise would be "learned" as a command.
        """
        if self._rx_pin is None:
            raise IrHardwareError("no IR receive pin is configured")
        await self._ensure_connected()
        if self._pi is None:
            raise IrHardwareError(self._error or "pigpiod not reachable")
        if self._recorder is not None:
            raise IrBusy("the IR receiver is already capturing")

        recorder = _FrameRecorder(gap_us)
        self._recorder = recorder
        try:
            timings = await asyncio.wait_for(recorder.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise IrTimeout("no signal was received -- point the remote at the receiver and try again")
        finally:
            self._recorder = None

        if len(timings) < MIN_PULSES:
            raise IrHardwareError(
                f"only {len(timings)} pulse(s) seen -- try holding the remote closer and pressing firmly"
            )
        return timings

    # -- transmit -----------------------------------------------------------

    async def transmit(
        self,
        timings: Sequence[int],
        carrier_hz: int = DEFAULT_CARRIER_HZ,
        *,
        duty_cycle: float = DEFAULT_DUTY_CYCLE,
        repeats: int = 1,
        gap_ms: float = DEFAULT_GAP_MS,
    ) -> None:
        """Plays `timings` back through the LED, `repeats` times.

        Serialised on the gateway's own lock and spaced by `gap_ms` from
        whatever was sent last -- there is one LED, and two overlapping
        waveforms would be noise, not two commands. The heavy lifting
        (building the waveform, handing it to pigpio, waiting for it to
        finish playing) runs in a worker thread: pigpio's calls are blocking
        socket I/O to `pigpiod`, and stalling the event loop here would also
        stall the remote's own event stream.
        """
        if self._tx_pin is None:
            raise IrHardwareError("no IR transmit pin is configured")
        await self._ensure_connected()
        if self._pi is None:
            raise IrHardwareError(self._error or "pigpiod not reachable")

        async with self._send_lock:
            wait = gap_ms / 1000 - (time.monotonic() - self._last_send_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await asyncio.to_thread(
                    self._transmit_sync, list(timings), carrier_hz, duty_cycle, max(repeats, 1), gap_ms
                )
            finally:
                self._last_send_at = time.monotonic()

    def _transmit_sync(
        self, timings: List[int], carrier_hz: int, duty_cycle: float, repeats: int, gap_ms: float
    ) -> None:
        """Builds and plays the waveform. Runs on a worker thread -- see `transmit`."""
        import pigpio

        mask = 1 << self._tx_pin
        cycle_us = 1_000_000.0 / carrier_hz
        on_us = max(cycle_us * duty_cycle, 1.0)
        off_us = max(cycle_us - on_us, 1.0)

        def mark_pulses(duration: int) -> List[Any]:
            # A mark is on-carrier: alternating on/off micro-pulses at
            # `carrier_hz`, for the mark's whole duration. A space is simply
            # off for its duration, handled by the caller.
            #
            # Each sub-pulse is rounded to a whole microsecond for pigpio,
            # and `on_us`/`off_us` are rarely whole numbers themselves
            # (38kHz at 33% duty is 8.684/17.632) -- rounding every one of
            # them independently would round the same fraction the same way
            # every single cycle, a systematic drift rather than the random
            # jitter naive rounding suggests. `carry` is the standard fix:
            # each pulse's rounding error is folded into the next one, so
            # the running total tracks the ideal duration indefinitely
            # rather than drifting further from it with every extra cycle.
            pulses: List[Any] = []
            remaining = float(duration)
            carry = 0.0
            on_phase = True
            while remaining > 0.5:
                target = min(on_us if on_phase else off_us, remaining)
                ideal = target + carry
                emitted = max(round(ideal), 1)
                carry = ideal - emitted
                pulses.append(
                    pigpio.pulse(mask, 0, emitted) if on_phase else pigpio.pulse(0, mask, emitted)
                )
                remaining -= target
                on_phase = not on_phase
            return pulses

        frame: List[Any] = []
        for index, duration in enumerate(timings):
            if index % 2 == 0:
                frame.extend(mark_pulses(duration))
            else:
                frame.append(pigpio.pulse(0, mask, max(int(duration), 1)))

        pulses: List[Any] = []
        for i in range(repeats):
            pulses.extend(frame)
            if i < repeats - 1:
                pulses.append(pigpio.pulse(0, mask, max(int(gap_ms * 1000), 1)))

        if len(pulses) > MAX_WAVE_PULSES:
            raise IrHardwareError(f"code too long to transmit ({len(pulses)} pulses)")

        pi = self._pi
        self._muted = True
        try:
            pi.wave_add_generic(pulses)
            wave_id = pi.wave_create()
            try:
                pi.wave_send_once(wave_id)
                while pi.wave_tx_busy():
                    time.sleep(0.001)
            finally:
                pi.wave_delete(wave_id)
        finally:
            time.sleep(MUTE_TAIL_S)
            self._muted = False


# --------------------------------------------------------------------------
# Process-wide singleton
# --------------------------------------------------------------------------

_GATEWAY = IrGateway()


def gateway() -> IrGateway:
    """The one gateway for this process. Configure it with `reconfigure` first."""
    return _GATEWAY


def reconfigure(settings: "HubSettings") -> None:
    """Applies `settings`' IR pins to the shared gateway.

    Called once at hub startup and again on every settings save whose IR
    fields changed -- see `HubRuntime.apply_settings`. Cheap and
    self-contained: no backend is rebuilt and nothing about the hub
    restarts, which is what lets the pins be changed on the fly.
    """
    _GATEWAY.configure(settings.ir_rx_pin, settings.ir_tx_pin, settings.ir_pigpio_host, settings.ir_pigpio_port)
