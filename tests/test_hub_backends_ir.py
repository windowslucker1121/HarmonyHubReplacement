"""The IR backend, driven against a fake gateway rather than real hardware.

`FakeGateway` stands in for `harmony_hub.ir.gateway.IrGateway` wherever the
backend or the shared learn job needs one -- the same shape
`test_hub_backends_denon.py` uses an `httpx.MockTransport` for. Actual pigpio
interaction (waveform building, edge capture, a fake `pigpio` module) is
covered separately in `test_ir_gateway.py`; this file is about the backend
and the learn-job state machine sitting on top of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harmony_hub import backends
from harmony_hub.backends import BackendError
from harmony_hub.backends.ir import IrBackend
from harmony_hub.ir import codes as ir_codes
from harmony_hub.ir import learn as learn_module
from harmony_hub.ir.gateway import IrHardwareError, IrTimeout

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeGateway:
    """A stand-in for `IrGateway`: no pins, no pigpio, just recorded calls."""

    def __init__(self, rx_ready: bool = True, tx_ready: bool = True) -> None:
        self.rx_ready = rx_ready
        self.tx_ready = tx_ready
        # `rx_configured`/`tx_configured` mean "a pin is set" independent of
        # "and pigpiod is reachable" (`rx_ready`/`tx_ready`) -- see the real
        # `IrGateway`'s docstring on the two. A fake with no pin configured
        # at all is the same as one that just hasn't connected yet for every
        # test here, so these default to whatever `rx_ready`/`tx_ready` says
        # unless a test explicitly wants to tell the two apart.
        self.rx_configured = rx_ready
        self.tx_configured = tx_ready
        self.sent: list[dict] = []
        self._captures: list = []
        self._healthy = True
        self._health_detail = "receive GPIO17, transmit GPIO18"

    def shutdown(self) -> None:
        pass

    def queue_capture(self, result) -> None:
        self._captures.append(result)

    async def capture(self, timeout: float, gap_us: int = 10_000):
        if not self.rx_configured:
            raise IrHardwareError("no IR receive pin is configured")
        if not self.rx_ready:
            # Mirrors the real gateway's `_ensure_connected` failing again --
            # a pin that *is* set but whose pigpiod connection is down.
            raise IrHardwareError("pigpiod not reachable at localhost:8888")
        if not self._captures:
            raise IrTimeout("no signal was received")
        result = self._captures.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def transmit(self, timings, carrier_hz, *, duty_cycle=0.33, repeats=1, gap_ms=40.0) -> None:
        if not self.tx_configured:
            raise IrHardwareError("no IR transmit pin is configured")
        if not self.tx_ready:
            raise IrHardwareError("pigpiod not reachable at localhost:8888")
        self.sent.append(
            {"timings": list(timings), "carrier_hz": carrier_hz, "duty_cycle": duty_cycle, "repeats": repeats}
        )

    def health(self):
        return self._healthy, self._health_detail


@pytest.fixture(autouse=True)
def _reset_shared_learn_job():
    """The learn job is a process-wide singleton -- see `ir/learn.py`'s docstring
    on why -- so it must not leak ownership from one test into the next."""
    learn_module.job()._reset()
    yield
    learn_module.job()._reset()


def make_backend(tmp_path: Path, device_id: str = "living_room_tv", **config) -> IrBackend:
    config.setdefault("codes_dir", str(tmp_path / "codes"))
    return IrBackend(device_id, config)


def install_fake_gateway(monkeypatch, **kwargs) -> FakeGateway:
    fake = FakeGateway(**kwargs)
    monkeypatch.setattr("harmony_hub.backends.ir.ir_gateway.gateway", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Registration and the device form
# ---------------------------------------------------------------------------


def test_the_backend_registers_under_its_own_name():
    assert backends.get("ir") is IrBackend


def test_it_is_learnable_but_not_pairable():
    assert issubclass(IrBackend, backends.Learnable)
    assert not issubclass(IrBackend, backends.Pairable)


def test_the_device_form_only_asks_for_per_equipment_settings_not_pins():
    schema = IrBackend.config_schema()
    properties = schema["properties"]
    assert "carrier_hz" in properties
    assert "duty_cycle" in properties
    # The pins are install-wide (`HubSettings.ir_rx_pin`/`ir_tx_pin`), not
    # per-device -- see the module docstring.
    assert "rx_pin" not in properties
    assert "tx_pin" not in properties


# ---------------------------------------------------------------------------
# Staying usable with nothing wired
# ---------------------------------------------------------------------------


async def test_a_device_with_no_gateway_reachable_still_lists_its_commands(tmp_path, monkeypatch):
    install_fake_gateway(monkeypatch, rx_ready=False, tx_ready=False)
    backend = make_backend(tmp_path)

    await backend.connect()  # must not raise

    assert await backend.commands() == []


async def test_a_codes_file_that_will_not_parse_is_treated_as_nothing_learned(tmp_path):
    codes_dir = tmp_path / "codes"
    codes_dir.mkdir()
    (codes_dir / "ir_living_room_tv.json").write_text("{ broken", encoding="utf-8")
    backend = make_backend(tmp_path)

    await backend.connect()  # must not raise

    assert await backend.commands() == []


async def test_commands_come_from_the_saved_codeset(tmp_path):
    codeset = ir_codes.CodeSet()
    codeset.add("volume_up", "Volume Up", [560, 1690], repeatable=True, decoded="NEC 0x04 0x08")
    codeset.save(ir_codes.path_for("living_room_tv", str(tmp_path / "codes")))

    backend = make_backend(tmp_path)
    await backend.connect()

    commands = await backend.commands()
    assert len(commands) == 1
    assert commands[0].name == "volume_up"
    assert commands[0].repeatable is True
    assert commands[0].description == "NEC 0x04 0x08"


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


async def test_an_unknown_command_is_refused_with_the_known_ones_named(tmp_path):
    codeset = ir_codes.CodeSet()
    codeset.add("power_toggle", "Power", [1, 2])
    codeset.save(ir_codes.path_for("tv", str(tmp_path / "codes")))
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    with pytest.raises(BackendError, match="power_toggle"):
        await backend.send("volume_up")


async def test_sending_plays_the_learned_timings_through_the_gateway(tmp_path, monkeypatch):
    fake = install_fake_gateway(monkeypatch)
    codeset = ir_codes.CodeSet()
    codeset.add("volume_up", "Volume Up", [560, 1690], repeats=2, repeatable=True)
    codeset.save(ir_codes.path_for("tv", str(tmp_path / "codes")))
    backend = make_backend(tmp_path, device_id="tv", carrier_hz=36000)
    await backend.connect()

    await backend.send("volume_up")

    assert len(fake.sent) == 1
    assert fake.sent[0]["timings"] == [560, 1690]
    assert fake.sent[0]["carrier_hz"] == 36000
    assert fake.sent[0]["repeats"] == 2  # the command's own override wins


async def test_a_hardware_failure_while_sending_is_a_backend_error(tmp_path, monkeypatch):
    fake = install_fake_gateway(monkeypatch, tx_ready=False)
    codeset = ir_codes.CodeSet()
    codeset.add("power_on", "Power on", [1, 2])
    codeset.save(ir_codes.path_for("tv", str(tmp_path / "codes")))
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    with pytest.raises(BackendError):
        await backend.send("power_on")


async def test_health_combines_the_gateways_and_the_codesets_own_status(tmp_path, monkeypatch):
    fake = install_fake_gateway(monkeypatch)
    fake._healthy = True
    fake._health_detail = "receive GPIO17, transmit GPIO18"
    codeset = ir_codes.CodeSet()
    codeset.add("power_on", "Power on", [1, 2])
    codeset.save(ir_codes.path_for("tv", str(tmp_path / "codes")))
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    health = await backend.health()

    assert health.ok is True
    assert "1 code(s) learned" in health.detail


def test_the_receiver_never_steals_the_smarthome_keys(tmp_path):
    backend = make_backend(tmp_path)
    assert backend.focus_for("volume_up") is None


# ---------------------------------------------------------------------------
# Suggested bindings
# ---------------------------------------------------------------------------


async def test_suggested_bindings_map_every_learned_command_onto_itself(tmp_path):
    codeset = ir_codes.CodeSet()
    codeset.add("volume_up", "Volume Up", [1, 2])
    codeset.add("channel_down", "Channel Down", [3, 4])
    codeset.save(ir_codes.path_for("tv", str(tmp_path / "codes")))
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    assert backend.suggested_bindings() == {"volume_up": "volume_up", "channel_down": "channel_down"}


def test_every_name_a_suggestion_could_offer_is_a_real_button_key():
    """The names the learn screen offers to pick from must themselves be real
    -- see `ir_learn_screen.dart` -- which is what makes `suggested_bindings`
    trustworthy without a fixed lookup table the way `denon`'s is."""
    import json

    buttons = json.loads((REPO_ROOT / "buttons.json").read_text(encoding="utf-8"))
    for name in ("volume_up", "volume_down", "channel_up", "channel_down", "mute"):
        assert name in buttons


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


async def test_learning_with_no_receive_pin_fails_immediately(tmp_path, monkeypatch):
    install_fake_gateway(monkeypatch, rx_ready=False)
    backend = make_backend(tmp_path)

    status = await backend.learn_start(timeout=1.0)

    assert status.state == "failed"
    assert "no IR receive pin" in status.detail
    assert learn_module.job().owner is None  # never claimed the receiver


async def test_two_agreeing_captures_are_learned_and_saved(tmp_path, monkeypatch):
    fake = install_fake_gateway(monkeypatch)
    fake.queue_capture([9000, 4500, 560, 1690])
    fake.queue_capture([9012, 4488, 561, 1688])  # jittered, but should agree
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    status = await backend.learn_start(timeout=1.0)
    assert status.state == "waiting"
    await learn_module.job()._task

    final = backend.learn_status()
    assert final.state == "captured"
    assert final.pulses == 4

    await backend.learn_save("volume_up", "Volume Up", repeatable=True)

    commands = await backend.commands()
    assert [c.name for c in commands] == ["volume_up"]
    assert commands[0].repeatable is True
    # And it is genuinely on disk, not just in memory.
    reloaded = ir_codes.CodeSet.load(ir_codes.path_for("tv", str(tmp_path / "codes")))
    assert "volume_up" in reloaded


async def test_disagreeing_captures_are_reported_as_a_mismatch_not_saved(tmp_path, monkeypatch):
    fake = install_fake_gateway(monkeypatch)
    fake.queue_capture([9000, 4500, 560, 1690])
    fake.queue_capture([9000, 4500, 560, 560])  # genuinely different
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    await backend.learn_start(timeout=1.0)
    await learn_module.job()._task

    assert backend.learn_status().state == "mismatch"
    assert await backend.commands() == []


async def test_no_signal_before_the_timeout_is_reported_as_failed(tmp_path, monkeypatch):
    install_fake_gateway(monkeypatch)  # no captures queued -> IrTimeout
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    await backend.learn_start(timeout=1.0)
    await learn_module.job()._task

    assert backend.learn_status().state == "failed"


async def test_a_second_device_is_refused_while_one_is_learning(tmp_path, monkeypatch):
    install_fake_gateway(monkeypatch)
    first = make_backend(tmp_path, device_id="tv")
    second = make_backend(tmp_path, device_id="soundbar")
    await first.connect()
    await second.connect()

    await first.learn_start(timeout=5.0)

    with pytest.raises(BackendError, match="tv"):
        await second.learn_start(timeout=5.0)

    # Cleanly cancel so nothing is left hanging for other tests.
    first.learn_cancel()


async def test_the_owning_device_can_restart_its_own_job(tmp_path, monkeypatch):
    fake = install_fake_gateway(monkeypatch)
    fake.queue_capture(IrTimeout("no signal"))
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    await backend.learn_start(timeout=1.0)
    # A fresh attempt for the same device, before the first one even resolves.
    status = await backend.learn_start(timeout=1.0)

    assert status.state == "waiting"


async def test_a_pin_that_is_set_but_unreachable_still_reaches_the_job(tmp_path, monkeypatch):
    """The distinction `IrBackend.learn_start` makes: a pin that was never
    configured is refused instantly and by name; a pin that *is* configured
    but whose pigpiod connection is currently down is not pre-judged -- the
    job is attempted, and reports the real, specific reason once it fails,
    rather than the generic "no pin configured" message that used to show
    even when a pin genuinely was set (the bug this guards against)."""
    fake = install_fake_gateway(monkeypatch, rx_ready=False)
    fake.rx_configured = True  # the pin is set; only the daemon is unreachable
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    status = await backend.learn_start(timeout=1.0)
    assert status.state == "waiting"  # not an eager "failed"

    await learn_module.job()._task

    final = backend.learn_status()
    assert final.state == "failed"
    assert "pigpiod not reachable" in final.detail
    assert "no IR receive pin" not in final.detail


async def test_a_failed_job_releases_the_receiver_for_a_different_device(tmp_path, monkeypatch):
    install_fake_gateway(monkeypatch)  # nothing queued -> IrTimeout -> "failed"
    first = make_backend(tmp_path, device_id="tv")
    second = make_backend(tmp_path, device_id="soundbar")
    await first.connect()
    await second.connect()

    await first.learn_start(timeout=1.0)
    await learn_module.job()._task
    assert first.learn_status().state == "failed"

    status = await second.learn_start(timeout=1.0)
    assert status.state == "waiting"
    second.learn_cancel()


async def test_a_mismatched_job_releases_the_receiver_for_a_different_device(tmp_path, monkeypatch):
    fake = install_fake_gateway(monkeypatch)
    fake.queue_capture([9000, 4500, 560, 1690])
    fake.queue_capture([9000, 4500, 560, 560])  # genuinely different
    first = make_backend(tmp_path, device_id="tv")
    second = make_backend(tmp_path, device_id="soundbar")
    await first.connect()
    await second.connect()

    await first.learn_start(timeout=1.0)
    await learn_module.job()._task
    assert first.learn_status().state == "mismatch"

    status = await second.learn_start(timeout=1.0)
    assert status.state == "waiting"
    second.learn_cancel()


async def test_an_unsaved_capture_still_blocks_a_different_device(tmp_path, monkeypatch):
    """A `"captured"` result is not a failure -- someone may still come back
    to save it, so unlike `"failed"`/`"mismatch"` it keeps the receiver."""
    fake = install_fake_gateway(monkeypatch)
    fake.queue_capture([9000, 4500, 560, 1690])
    fake.queue_capture([9012, 4488, 561, 1688])
    first = make_backend(tmp_path, device_id="tv")
    second = make_backend(tmp_path, device_id="soundbar")
    await first.connect()
    await second.connect()

    await first.learn_start(timeout=1.0)
    await learn_module.job()._task
    assert first.learn_status().state == "captured"

    with pytest.raises(BackendError, match="tv"):
        await second.learn_start(timeout=1.0)

    first.learn_cancel()


async def test_verifying_before_a_capture_exists_is_refused(tmp_path, monkeypatch):
    install_fake_gateway(monkeypatch)
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    with pytest.raises(BackendError, match="nothing has been captured"):
        await backend.learn_verify()


async def test_verify_replays_the_captured_timings(tmp_path, monkeypatch):
    fake = install_fake_gateway(monkeypatch)
    fake.queue_capture([9000, 4500, 560, 1690])
    fake.queue_capture([9000, 4500, 560, 1690])
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    await backend.learn_start(timeout=1.0)
    await learn_module.job()._task
    await backend.learn_verify()

    assert len(fake.sent) == 1
    assert fake.sent[0]["timings"] == [9000, 4500, 560, 1690]


async def test_learn_cancel_frees_the_receiver_for_another_device(tmp_path, monkeypatch):
    install_fake_gateway(monkeypatch)
    first = make_backend(tmp_path, device_id="tv")
    second = make_backend(tmp_path, device_id="soundbar")
    await first.connect()
    await second.connect()

    await first.learn_start(timeout=5.0)
    first.learn_cancel()

    status = await second.learn_start(timeout=1.0)
    assert status.state == "waiting"
    second.learn_cancel()


async def test_learn_forget_removes_a_command_and_persists(tmp_path):
    codeset = ir_codes.CodeSet()
    codeset.add("mute", "Mute", [1, 2])
    codeset.save(ir_codes.path_for("tv", str(tmp_path / "codes")))
    backend = make_backend(tmp_path, device_id="tv")
    await backend.connect()

    await backend.learn_forget("mute")

    assert await backend.commands() == []
    reloaded = ir_codes.CodeSet.load(ir_codes.path_for("tv", str(tmp_path / "codes")))
    assert "mute" not in reloaded
