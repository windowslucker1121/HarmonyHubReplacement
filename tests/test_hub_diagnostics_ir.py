"""`_check_ir`: the install-wide IR wiring sanity check.

Distinct from a device's own `health()` (covered by `_check_devices`, which
needs a running hub) -- this is settings- and file-based, so it works with
the hub stopped and even before a single IR device has been configured. See
`diagnostics.py`'s docstring on `_check_ir` for why it exists separately.
"""

from __future__ import annotations

import json

from harmony_hub.diagnostics import _check_ir
from harmony_hub.ir.codes import CodeSet, path_for
from harmony_hub.runtime import HubRuntime
from harmony_hub.settings import HubSettings


def make_runtime(paths, **overrides) -> HubRuntime:
    settings = HubSettings(
        config_path=paths / "hub_config.json",
        buttons_path=paths / "buttons.json",
        **overrides,
    )
    return HubRuntime(settings, settings_path=paths / "hub_settings.json")


def test_omitted_entirely_when_nothing_is_wired_or_configured(tmp_path):
    runtime = make_runtime(tmp_path)
    assert _check_ir(runtime) is None


def test_a_receive_only_install_is_ok_but_notes_sending_will_not_work(tmp_path):
    runtime = make_runtime(tmp_path, ir_rx_pin=17)
    check = _check_ir(runtime)
    assert check is not None
    assert check.ok is True
    assert "receive GPIO17" in check.detail
    assert "sending does not" in check.detail


def test_a_transmit_only_install_notes_learning_will_not_work(tmp_path):
    runtime = make_runtime(tmp_path, ir_tx_pin=18)
    check = _check_ir(runtime)
    assert check.ok is True
    assert "transmit GPIO18" in check.detail
    assert "learning does not" in check.detail


def test_fully_wired_with_no_devices_reports_the_wiring_alone(tmp_path):
    runtime = make_runtime(tmp_path, ir_rx_pin=17, ir_tx_pin=18)
    check = _check_ir(runtime)
    assert check.ok is True
    assert "receive GPIO17" in check.detail
    assert "transmit GPIO18" in check.detail
    assert "device(s)" not in check.detail


def test_the_same_pin_for_both_directions_is_reported_as_blocking(tmp_path):
    runtime = make_runtime(tmp_path, ir_rx_pin=17, ir_tx_pin=17)
    check = _check_ir(runtime)
    assert check.ok is False
    assert "both GPIO17" in check.detail


def test_an_ir_pin_colliding_with_the_radios_ce_pin_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("harmony_hub.diagnostics.platform.system", lambda: "Linux")
    runtime = make_runtime(tmp_path, ir_rx_pin=6, ir_tx_pin=18, ce_pin="D6", csn_pin="D5")
    check = _check_ir(runtime)
    assert check.ok is False
    assert "GPIO6" in check.detail
    assert "radio" in check.detail


def test_the_same_collision_is_not_reported_off_linux(tmp_path, monkeypatch):
    monkeypatch.setattr("harmony_hub.diagnostics.platform.system", lambda: "Windows")
    runtime = make_runtime(tmp_path, ir_rx_pin=6, ir_tx_pin=18, ce_pin="D6", csn_pin="D5")
    check = _check_ir(runtime)
    assert check.ok is True


def test_an_ir_pin_on_the_pis_own_spi0_bus_is_reported_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr("harmony_hub.diagnostics.platform.system", lambda: "Linux")
    runtime = make_runtime(tmp_path, ir_rx_pin=9, ir_tx_pin=18)
    check = _check_ir(runtime)
    assert check.ok is False
    assert "GPIO9" in check.detail
    assert "SPI0" in check.detail


def test_configured_ir_devices_are_counted_even_with_no_pins_wired(tmp_path):
    codes_dir_1 = str(tmp_path / "codes1")
    codes_dir_2 = str(tmp_path / "codes2")
    config = {
        "version": 1,
        "devices": [
            {"id": "tv", "name": "TV", "backend": "ir", "config": {"codes_dir": codes_dir_1}},
            {"id": "amp", "name": "Amp", "backend": "ir", "config": {"codes_dir": codes_dir_2}},
        ],
    }
    (tmp_path / "hub_config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "buttons.json").write_text("{}", encoding="utf-8")

    codeset = CodeSet()
    codeset.add("power_on", "Power on", [1, 2, 3])
    codeset.add("power_off", "Power off", [4, 5, 6])
    codeset.save(path_for("tv", codes_dir_1))

    other = CodeSet()
    other.add("mute", "Mute", [7, 8])
    other.save(path_for("amp", codes_dir_2))

    runtime = make_runtime(tmp_path, ir_rx_pin=17, ir_tx_pin=18)
    check = _check_ir(runtime)
    assert check.ok is True
    assert "3 code(s) across 2 device(s)" in check.detail


# ---------------------------------------------------------------------------
# Live gateway health, surfaced here rather than only in a per-device row --
# see `_check_ir`'s inline comment on why: wiring being sane on paper does
# not mean the daemon is actually reachable, and this row used to say `ok`
# regardless, which is exactly what happened on a real device with pigpiod
# never started.
# ---------------------------------------------------------------------------


def test_a_stopped_hub_does_not_probe_the_live_gateway(tmp_path):
    # `runtime.service` is `None` here (the hub is stopped), and the gateway
    # was released on the last stop -- probing it would misreport "not
    # configured" even though the wiring above is genuinely fine.
    runtime = make_runtime(tmp_path, ir_rx_pin=17, ir_tx_pin=18)
    check = _check_ir(runtime)
    assert check.ok is True


def test_a_running_hub_with_an_unreachable_daemon_is_reported_as_not_ok(tmp_path, monkeypatch):
    class FakeGateway:
        def health(self):
            return False, "pigpiod not reachable at localhost:8888"

    monkeypatch.setattr("harmony_hub.diagnostics.ir_gateway.gateway", lambda: FakeGateway())
    runtime = make_runtime(tmp_path, ir_rx_pin=17, ir_tx_pin=18)
    runtime.service = object()  # stand-in for "the hub is running"

    check = _check_ir(runtime)

    assert check.ok is False
    assert "pigpiod not reachable" in check.detail
    assert "systemctl enable --now pigpiod" in check.detail


def test_a_running_hub_with_a_healthy_gateway_stays_ok(tmp_path, monkeypatch):
    class FakeGateway:
        def health(self):
            return True, "receive GPIO17, transmit GPIO18"

    monkeypatch.setattr("harmony_hub.diagnostics.ir_gateway.gateway", lambda: FakeGateway())
    runtime = make_runtime(tmp_path, ir_rx_pin=17, ir_tx_pin=18)
    runtime.service = object()

    check = _check_ir(runtime)
    assert check.ok is True


def test_no_pins_wired_is_never_probed_even_with_the_hub_running(tmp_path, monkeypatch):
    calls = []

    class FakeGateway:
        def health(self):
            calls.append(1)
            return False, "should never be asked"

    monkeypatch.setattr("harmony_hub.diagnostics.ir_gateway.gateway", lambda: FakeGateway())
    config = {"version": 1, "devices": [{"id": "tv", "name": "TV", "backend": "ir", "config": {}}]}
    (tmp_path / "hub_config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "buttons.json").write_text("{}", encoding="utf-8")
    runtime = make_runtime(tmp_path)  # no pins set at all
    runtime.service = object()

    check = _check_ir(runtime)
    assert calls == []
    assert check.ok is True
