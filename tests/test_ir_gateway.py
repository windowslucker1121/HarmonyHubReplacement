"""`IrGateway`, driven against a fake `pigpio` rather than real hardware.

`gateway.py` only ever imports `pigpio` from inside function bodies (never
at module scope), which is what makes it importable, and safe to construct
and configure, on a machine with nothing installed or wired -- exactly the
same shape `harmony_receiver.radio` uses for Blinka. Every test here injects
a fake `pigpio` module through `sys.modules` before exercising that code, the
same way `test_hub_backends_denon.py` substitutes an `httpx.MockTransport`
for a real receiver.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from harmony_hub.ir.gateway import IrBusy, IrGateway, IrHardwareError, IrTimeout


class FakePulse:
    def __init__(self, gpio_on, gpio_off, delay):
        self.gpio_on = gpio_on
        self.gpio_off = gpio_off
        self.delay = delay


class FakeCallback:
    def __init__(self, gpio, edge, func):
        self.gpio = gpio
        self.edge = edge
        self.func = func
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakePi:
    """A stand-in for `pigpio.pi(...)`, recording everything it was told."""

    def __init__(self, host="localhost", port=8888, connected=True, on_send=None):
        self.host = host
        self.port = port
        self.connected = connected
        self.modes = {}
        self.pulls = {}
        self.writes = []
        self.callbacks = []
        self.waves = {}
        self.sent = []
        self.stopped = False
        self._next_wave_id = 0
        self._pending = None
        self._on_send = on_send

    def stop(self):
        self.stopped = True

    def set_mode(self, pin, mode):
        self.modes[pin] = mode

    def set_pull_up_down(self, pin, pud):
        self.pulls[pin] = pud

    def write(self, pin, value):
        self.writes.append((pin, value))

    def callback(self, gpio, edge, func):
        cb = FakeCallback(gpio, edge, func)
        self.callbacks.append(cb)
        return cb

    def wave_clear(self):
        self.waves.clear()

    def wave_add_generic(self, pulses):
        self._pending = list(pulses)

    def wave_create(self):
        wave_id = self._next_wave_id
        self._next_wave_id += 1
        self.waves[wave_id] = self._pending
        return wave_id

    def wave_send_once(self, wave_id):
        self.sent.append((wave_id, self.waves[wave_id]))
        if self._on_send is not None:
            self._on_send()

    def wave_tx_busy(self):
        return False

    def wave_delete(self, wave_id):
        self.waves.pop(wave_id, None)


def install_fake_pigpio(monkeypatch, connected=True, on_send=None):
    """Injects a fake `pigpio` module and returns the `FakePi` it will hand back."""
    fake_pi_holder = {}

    module = types.ModuleType("pigpio")
    module.INPUT = "INPUT"
    module.OUTPUT = "OUTPUT"
    module.PUD_UP = "PUD_UP"
    module.EITHER_EDGE = "EITHER_EDGE"
    module.pulse = FakePulse

    def pi(host="localhost", port=8888):
        instance = FakePi(host, port, connected=connected, on_send=on_send)
        fake_pi_holder["instance"] = instance
        return instance

    module.pi = pi
    monkeypatch.setitem(sys.modules, "pigpio", module)
    return fake_pi_holder


def configured_gateway(monkeypatch, *, rx_pin=17, tx_pin=18, connected=True, on_send=None) -> IrGateway:
    holder = install_fake_pigpio(monkeypatch, connected=connected, on_send=on_send)
    gw = IrGateway()
    gw.configure(rx_pin, tx_pin, "localhost", 8888)
    gw._fake_pi = holder.get("instance")  # for the test's own inspection
    return gw


# ---------------------------------------------------------------------------
# configure / shutdown / health
# ---------------------------------------------------------------------------


def test_unwired_pins_are_healthy_and_idle(monkeypatch):
    gw = configured_gateway(monkeypatch, rx_pin=None, tx_pin=None)
    ok, detail = gw.health()
    assert ok is True
    assert "no pins" in detail
    assert gw._fake_pi is None  # never even tried to connect


def test_an_unreachable_daemon_is_recorded_not_raised(monkeypatch):
    gw = configured_gateway(monkeypatch, connected=False)
    ok, detail = gw.health()
    assert ok is False
    assert "not reachable" in detail


def test_configure_never_raises_with_no_pigpio_module_installed(monkeypatch):
    # No fake is installed here, and this dev venv genuinely has no `pigpio`
    # package -- so this exercises the real "not installed" path `import
    # pigpio` takes inside `connect_pigpio`, not a simulation of it.
    monkeypatch.delitem(sys.modules, "pigpio", raising=False)
    gw = IrGateway()
    gw.configure(17, 18, "localhost", 8888)  # must not raise
    ok, detail = gw.health()
    assert ok is False


def test_a_reachable_daemon_sets_up_rx_as_input_and_tx_as_output(monkeypatch):
    gw = configured_gateway(monkeypatch, rx_pin=17, tx_pin=18)
    pi = gw._fake_pi
    assert pi.modes[17] == "INPUT"
    assert pi.pulls[17] == "PUD_UP"
    assert pi.modes[18] == "OUTPUT"
    assert (18, 0) in pi.writes
    ok, detail = gw.health()
    assert ok is True
    assert "receive GPIO17" in detail
    assert "transmit GPIO18" in detail


def test_reconfiguring_cancels_the_previous_callback_rather_than_leaking_it(monkeypatch):
    gw = configured_gateway(monkeypatch, rx_pin=17, tx_pin=18)
    first_callback = gw._callback
    assert first_callback.cancelled is False

    install_fake_pigpio(monkeypatch)  # a fresh daemon connection for the new pins
    gw.configure(27, 18, "localhost", 8888)

    assert first_callback.cancelled is True


def test_shutdown_is_safe_to_call_twice(monkeypatch):
    gw = configured_gateway(monkeypatch)
    gw.shutdown()
    gw.shutdown()  # must not raise
    assert gw._pi is None


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


async def _feed_edges(cb: FakeCallback, deltas, start_tick=1_000):
    await asyncio.sleep(0)  # let capture() register its recorder first
    tick = start_tick
    cb.func(cb.gpio, 0, tick)
    for delta in deltas:
        tick += delta
        cb.func(cb.gpio, 0, tick)


async def test_capture_returns_the_deltas_between_edges(monkeypatch):
    gw = configured_gateway(monkeypatch, tx_pin=None)
    cb = gw._callback
    deltas = [9000, 4500, 560, 560, 560, 1690, 560, 1690]

    result, _ = await asyncio.gather(
        gw.capture(timeout=1.0, gap_us=2_000),
        _feed_edges(cb, deltas),
    )
    assert result == deltas


async def test_capture_times_out_with_no_signal(monkeypatch):
    gw = configured_gateway(monkeypatch, tx_pin=None)
    with pytest.raises(IrTimeout):
        await gw.capture(timeout=0.05)


async def test_a_capture_with_too_few_pulses_is_rejected_as_noise(monkeypatch):
    gw = configured_gateway(monkeypatch, tx_pin=None)
    cb = gw._callback
    with pytest.raises(IrHardwareError):
        await asyncio.gather(
            gw.capture(timeout=1.0, gap_us=2_000),
            _feed_edges(cb, [560, 560]),  # far under MIN_PULSES
        )


async def test_a_second_concurrent_capture_is_refused(monkeypatch):
    gw = configured_gateway(monkeypatch, tx_pin=None)
    first = asyncio.ensure_future(gw.capture(timeout=1.0))
    await asyncio.sleep(0)  # let it claim `_recorder`
    try:
        with pytest.raises(IrBusy):
            await gw.capture(timeout=1.0)
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first


async def test_capturing_with_no_receive_pin_configured_is_a_clear_error(monkeypatch):
    gw = configured_gateway(monkeypatch, rx_pin=None)
    with pytest.raises(IrHardwareError, match="no IR receive pin"):
        await gw.capture(timeout=0.1)


def test_edges_are_ignored_while_a_transmission_is_in_progress(monkeypatch):
    gw = configured_gateway(monkeypatch, tx_pin=None)
    seen = []

    class _Recorder:
        def on_edge(self, tick):
            seen.append(tick)

    gw._recorder = _Recorder()
    gw._muted = True
    gw._on_edge(17, 0, 42)
    assert seen == []

    gw._muted = False
    gw._on_edge(17, 0, 43)
    assert seen == [43]


# ---------------------------------------------------------------------------
# transmit
# ---------------------------------------------------------------------------


async def test_transmit_with_no_transmit_pin_configured_is_a_clear_error(monkeypatch):
    gw = configured_gateway(monkeypatch, tx_pin=None)
    with pytest.raises(IrHardwareError, match="no IR transmit pin"):
        await gw.transmit([560, 560])


async def test_transmit_mutes_the_receiver_only_for_the_duration_of_the_send(monkeypatch):
    observed = {}
    gw = configured_gateway(monkeypatch)
    gw._fake_pi._on_send = lambda: observed.setdefault("muted_during_send", gw._muted)

    await gw.transmit([560, 560, 560, 560], carrier_hz=38_000, gap_ms=1)

    assert observed["muted_during_send"] is True
    assert gw._muted is False


async def test_carrier_expansion_preserves_total_airtime_within_rounding(monkeypatch):
    gw = configured_gateway(monkeypatch, rx_pin=None)
    await gw.transmit([1000, 500, 2000], carrier_hz=38_000, duty_cycle=0.33, gap_ms=1)

    wave_id, pulses = gw._fake_pi.sent[0]
    total = sum(p.delay for p in pulses)
    assert abs(total - 3500) <= 10


async def test_a_code_too_long_to_fit_one_wave_is_refused_before_touching_hardware(monkeypatch):
    gw = configured_gateway(monkeypatch, rx_pin=None)
    with pytest.raises(IrHardwareError, match="too long"):
        await gw.transmit([5_000_000], carrier_hz=38_000)
    assert gw._fake_pi.waves == {}
    assert gw._fake_pi.sent == []


async def test_repeats_are_separated_by_the_configured_gap(monkeypatch):
    gw = configured_gateway(monkeypatch, rx_pin=None)
    await gw.transmit([560, 560], carrier_hz=38_000, repeats=3, gap_ms=40)

    _, pulses = gw._fake_pi.sent[0]
    gap_pulses = [p for p in pulses if p.gpio_on == 0 and p.delay >= 39_000]
    assert len(gap_pulses) == 2  # two gaps between three repeats


async def test_two_sends_are_spaced_by_at_least_the_configured_gap(monkeypatch):
    gw = configured_gateway(monkeypatch, rx_pin=None)
    loop = asyncio.get_event_loop()

    await gw.transmit([560, 560], carrier_hz=38_000, gap_ms=50)
    start = loop.time()
    await gw.transmit([560, 560], carrier_hz=38_000, gap_ms=50)
    elapsed = loop.time() - start

    assert elapsed >= 0.045  # a little under 50ms to absorb scheduling slack


# ---------------------------------------------------------------------------
# `rx_configured` / `tx_configured` -- "a pin is set" vs `rx_ready`/`tx_ready`,
# "a pin is set *and* connected". This is what a real Pi with `pigpiod` not
# yet started needs told apart -- see `IrBackend.learn_start`.
# ---------------------------------------------------------------------------


def test_configured_is_true_even_when_the_daemon_is_unreachable(monkeypatch):
    gw = configured_gateway(monkeypatch, rx_pin=17, tx_pin=18, connected=False)
    assert gw.rx_configured is True
    assert gw.tx_configured is True
    assert gw.rx_ready is False
    assert gw.tx_ready is False


def test_configured_is_false_with_no_pin_set(monkeypatch):
    gw = configured_gateway(monkeypatch, rx_pin=None, tx_pin=None)
    assert gw.rx_configured is False
    assert gw.tx_configured is False


# ---------------------------------------------------------------------------
# Lazy reconnect -- a `pigpiod` that was down at hub startup and has since
# been started must not require a hub restart to start working.
# ---------------------------------------------------------------------------


async def _wait_for_reconnect(holder, attempts=100):
    """Polls until `_ensure_connected`'s background thread has finished
    reconnecting and registered a receive callback."""
    for _ in range(attempts):
        instance = holder.get("instance")
        if instance is not None and instance.callbacks:
            return instance.callbacks[0]
        await asyncio.sleep(0.01)
    raise AssertionError("gateway never reconnected")


async def test_capture_reconnects_and_succeeds_once_pigpiod_comes_up(monkeypatch):
    holder = install_fake_pigpio(monkeypatch, connected=False)
    gw = IrGateway()
    gw.configure(17, None, "localhost", 8888)
    assert gw.health() == (False, "pigpiod not reachable at localhost:8888")

    # pigpiod has since been started on the device -- the next connection
    # attempt now succeeds.
    module = sys.modules["pigpio"]

    def reconnected_pi(host="localhost", port=8888):
        instance = FakePi(host, port, connected=True)
        holder["instance"] = instance
        return instance

    module.pi = reconnected_pi

    deltas = [9000, 4500, 560, 560, 560, 1690, 560, 1690]

    async def feed():
        cb = await _wait_for_reconnect(holder)
        tick = 1_000
        cb.func(cb.gpio, 0, tick)
        for delta in deltas:
            tick += delta
            cb.func(cb.gpio, 0, tick)

    result, _ = await asyncio.gather(gw.capture(timeout=2.0, gap_us=2_000), feed())

    assert result == deltas
    assert gw.health()[0] is True


async def test_transmit_reconnects_and_succeeds_once_pigpiod_comes_up(monkeypatch):
    install_fake_pigpio(monkeypatch, connected=False)
    gw = IrGateway()
    gw.configure(None, 18, "localhost", 8888)
    assert gw.health()[0] is False

    module = sys.modules["pigpio"]
    reconnected = {}

    def reconnected_pi(host="localhost", port=8888):
        instance = FakePi(host, port, connected=True)
        reconnected["instance"] = instance
        return instance

    module.pi = reconnected_pi

    await gw.transmit([560, 560], carrier_hz=38_000, gap_ms=1)

    assert reconnected["instance"].sent  # actually reached the reconnected pi
    assert gw.health()[0] is True


async def test_a_still_unreachable_daemon_stays_reported_as_such(monkeypatch):
    """Retrying is not the same as always succeeding -- a daemon that is
    genuinely still down must keep reporting the real reason."""
    gw = configured_gateway(monkeypatch, rx_pin=17, tx_pin=None, connected=False)

    with pytest.raises(IrHardwareError, match="not reachable"):
        await gw.capture(timeout=0.1)
