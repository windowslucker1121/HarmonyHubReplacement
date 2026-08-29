"""The supervisor: the hub can fail, restart and be reconfigured, and the process lives.

These are the tests that justify the whole `HubRuntime` layer. Before it,
every one of the failures below took uvicorn down with it, which meant the
settings page that could have explained the problem never loaded.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from harmony_hub import config as config_module
from harmony_hub import runtime as runtime_module
from harmony_hub.models import HubConfig
from harmony_hub.runtime import ConfigUnreadable, HubNotRunning, HubRuntime
from harmony_hub.settings import HubSettings

CONFIG = {
    "version": 1,
    "devices": [{"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": ["on", "off"]}}],
    "scenes": [{"id": "watch_tv", "name": "Watch TV", "devices": ["tv"]}],
}

BUTTONS = {"volume_up": {"label": "Volume Up", "signatures": ["C3E90000"]}}


@pytest.fixture
def paths(tmp_path):
    (tmp_path / "hub_config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    (tmp_path / "buttons.json").write_text(json.dumps(BUTTONS), encoding="utf-8")
    return tmp_path


def make_runtime(paths, **overrides) -> HubRuntime:
    settings = HubSettings(
        config_path=paths / "hub_config.json",
        buttons_path=paths / "buttons.json",
        **overrides,
    )
    return HubRuntime(settings, settings_path=paths / "hub_settings.json")


# ----------------------------------------------------------------------
# Failure is a state, not an exception
# ----------------------------------------------------------------------


async def test_a_hub_that_cannot_start_fails_visibly_instead_of_raising(paths):
    """The one behaviour this whole layer exists for.

    `source='radio'` with no address used to raise out of FastAPI's lifespan
    and stop uvicorn serving anything at all.
    """
    runtime = make_runtime(paths, source="radio")

    started = await runtime.start()

    assert started is False
    assert runtime.state == "failed"
    assert "no remote address" in runtime.detail
    assert runtime.service is None
    # And the configuration is still right there to be fixed.
    assert runtime.config.scenes[0].id == "watch_tv"


async def test_a_failed_start_leaves_no_half_built_engine_behind(paths, monkeypatch):
    """A retry must not be fighting a service that partly came up."""
    runtime = make_runtime(paths)

    def explode(settings):
        raise RuntimeError("radio caught fire")

    monkeypatch.setattr("harmony_hub.service.build_source", explode)
    assert await runtime.start() is False
    assert runtime.state == "failed"
    assert runtime.service is None

    monkeypatch.undo()
    assert await runtime.start() is True
    assert runtime.state == "running"
    await runtime.stop()


async def test_a_configuration_file_that_will_not_parse_still_leaves_a_running_hub(paths):
    (paths / "hub_config.json").write_text("{ broken", encoding="utf-8")

    runtime = make_runtime(paths)

    assert runtime.config_error is not None
    assert await runtime.start() is True
    assert runtime.state == "running"
    await runtime.stop()


async def test_an_unreadable_config_is_not_silently_overwritten(paths):
    """Saving the empty stand-in over a recoverable file would delete every scene in it."""
    (paths / "hub_config.json").write_text("{ broken", encoding="utf-8")
    runtime = make_runtime(paths)

    with pytest.raises(ConfigUnreadable):
        await runtime.save_config(HubConfig())

    assert (paths / "hub_config.json").read_text() == "{ broken"

    # Replacing it is allowed, but only when asked for explicitly.
    await runtime.save_config(HubConfig(), force=True)
    assert config_module.load(paths / "hub_config.json").scenes == []


async def test_a_button_map_that_will_not_parse_does_not_stop_the_hub(paths):
    (paths / "buttons.json").write_text("{ broken", encoding="utf-8")

    runtime = make_runtime(paths)

    assert len(runtime.buttons) == 0
    assert await runtime.start() is True
    await runtime.stop()


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


async def test_start_stop_and_restart_move_through_the_expected_states(paths):
    runtime = make_runtime(paths)
    assert runtime.state == "stopped"

    await runtime.start()
    assert runtime.state == "running" and runtime.started_at is not None

    await runtime.stop()
    assert runtime.state == "stopped" and runtime.service is None and runtime.started_at is None

    await runtime.restart()
    assert runtime.state == "running"
    await runtime.stop()


async def test_starting_an_already_running_hub_is_a_no_op(paths):
    runtime = make_runtime(paths)
    await runtime.start()
    service = runtime.service

    await runtime.start()

    assert runtime.service is service
    await runtime.stop()


async def test_stopping_a_stopped_hub_is_safe(paths):
    runtime = make_runtime(paths)

    await runtime.stop()

    assert runtime.state == "stopped"


async def test_the_event_log_survives_a_restart(paths):
    """The activity log is what someone reads to work out why they restarted it."""
    runtime = make_runtime(paths)
    await runtime.start()
    before = len(runtime.broker.history)
    assert before > 0

    await runtime.restart()

    assert len(runtime.broker.history) > before
    await runtime.stop()


async def test_simulating_a_press_needs_a_running_hub(paths):
    runtime = make_runtime(paths)

    with pytest.raises(HubNotRunning):
        runtime.simulate("volume_up")


# ----------------------------------------------------------------------
# Applying changes
# ----------------------------------------------------------------------


async def test_configuration_saves_while_the_hub_is_stopped(paths):
    """Editing scenes should not require the equipment to be reachable."""
    runtime = make_runtime(paths)
    updated = HubConfig.model_validate({**CONFIG, "scenes": []})

    await runtime.save_config(updated)

    assert config_module.load(paths / "hub_config.json").scenes == []
    # And the hub picks it up when it does start.
    await runtime.start()
    assert runtime.service.engine.config.scenes == []
    await runtime.stop()


async def test_saving_configuration_reaches_a_running_engine(paths):
    runtime = make_runtime(paths)
    await runtime.start()

    await runtime.save_config(HubConfig.model_validate({**CONFIG, "scenes": []}))

    assert runtime.service.engine.config.scenes == []
    await runtime.stop()


async def test_new_settings_are_persisted_and_can_restart_the_hub(paths, tmp_path):
    capture = tmp_path / "cap.jsonl"
    capture.write_text("", encoding="utf-8")
    runtime = make_runtime(paths)
    await runtime.start()

    updated = runtime.settings.model_copy(update={"source": "replay", "replay_path": capture})
    await runtime.apply_settings(updated, restart=True)

    assert runtime.state == "running"
    assert runtime.settings.source == "replay"
    assert json.loads((paths / "hub_settings.json").read_text())["source"] == "replay"
    await runtime.stop()


async def test_a_bind_change_is_saved_but_reported_as_pending(paths):
    """Rebinding the live listener would take this page's URL with it."""
    runtime = make_runtime(paths)

    await runtime.apply_settings(runtime.settings.model_copy(update={"port": 9999}))

    status = runtime.status()
    assert status.pending_restart is True
    assert status.port == 8765  # still where this process is actually listening
    assert runtime.settings.port == 9999  # but saved for next time


async def test_an_ir_pin_change_is_applied_immediately_with_no_restart(paths, monkeypatch):
    """The one thing that must never need `restart=True` -- see `apply_settings`'s docstring."""
    calls = []
    monkeypatch.setattr(runtime_module.ir_gateway, "reconfigure", lambda settings: calls.append(settings))

    runtime = make_runtime(paths)
    await runtime.start()
    calls.clear()  # HubService.start() itself configures the gateway once; not what's under test

    updated = runtime.settings.model_copy(update={"ir_rx_pin": 17, "ir_tx_pin": 18})
    await runtime.apply_settings(updated)  # no restart=True

    assert runtime.state == "running"  # never went through starting/stopped
    assert len(calls) == 1
    assert calls[0].ir_rx_pin == 17 and calls[0].ir_tx_pin == 18
    await runtime.stop()


async def test_an_ir_pin_change_is_not_applied_while_the_hub_is_stopped(paths, monkeypatch):
    """Nothing live to reconfigure yet -- the next start reads the saved settings fresh."""
    calls = []
    monkeypatch.setattr(runtime_module.ir_gateway, "reconfigure", lambda settings: calls.append(settings))

    runtime = make_runtime(paths)
    await runtime.apply_settings(runtime.settings.model_copy(update={"ir_rx_pin": 17}))

    assert calls == []
    assert runtime.settings.ir_rx_pin == 17  # still saved


async def test_saving_settings_with_no_ir_change_does_not_touch_the_gateway(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime_module.ir_gateway, "reconfigure", lambda settings: calls.append(settings))

    runtime = make_runtime(paths)
    await runtime.start()
    calls.clear()  # HubService.start() itself configures the gateway once; not what's under test
    await runtime.apply_settings(runtime.settings.model_copy(update={"probe_interval": 5.0}))

    assert calls == []
    await runtime.stop()


async def test_moving_the_config_path_reloads_from_the_new_file(paths, tmp_path):
    elsewhere = tmp_path / "other.json"
    elsewhere.write_text(json.dumps({**CONFIG, "scenes": []}), encoding="utf-8")
    runtime = make_runtime(paths)
    await runtime.start()

    await runtime.apply_settings(runtime.settings.model_copy(update={"config_path": elsewhere}))

    assert runtime.config.scenes == []
    assert runtime.service.engine.config.scenes == []
    await runtime.stop()


async def test_status_reports_what_the_page_needs_to_render(paths):
    runtime = make_runtime(paths, source="radio")
    await runtime.start()

    status = runtime.status()

    assert status.state == "failed"
    assert status.problems == ["Source is 'radio' but no remote address is set."]
    assert status.config_path.endswith("hub_config.json")


# ----------------------------------------------------------------------
# Address discovery
# ----------------------------------------------------------------------


async def _poll_until_finished(runtime) -> None:
    for _ in range(200):
        if runtime.discovery.status.state != "running":
            return
        await asyncio.sleep(0.05)


async def test_discovery_stops_a_radio_hub_and_starts_it_again_afterwards(paths, monkeypatch):
    """Two things driving one nRF24 would fail in a way that looks like a hardware fault,

    so rather than refuse the search outright, a radio hub is stopped for its
    duration and brought back up once it ends -- found, failed or cancelled.
    No FT232H is attached in CI, so the search itself fails fast; what this
    test cares about is that the hub still yields the radio and reclaims it.
    """
    from harmony_hub.sources import RadioSource

    class FakeReceiver:
        radio = None

    monkeypatch.setattr(
        "harmony_hub.service.build_source",
        lambda settings: RadioSource(FakeReceiver()),
    )
    runtime = make_runtime(paths, source="radio", address="17129BFCB6")
    await runtime.start()
    assert runtime.state == "running"

    status = await runtime.start_discovery(5.0)
    assert status.state == "running"
    # Stopping to free the radio happens before `start_discovery` returns,
    # not sometime later on the worker thread.
    assert runtime.state == "stopped"

    await _poll_until_finished(runtime)

    assert runtime.state == "running"


async def test_discovery_leaves_a_non_radio_hub_running(paths, monkeypatch):
    """Nothing to yield -- a manual/replay hub isn't touching the nRF24."""
    runtime = make_runtime(paths, source="none")
    await runtime.start()
    assert runtime.state == "running"

    await runtime.start_discovery(5.0)
    assert runtime.state == "running"

    await _poll_until_finished(runtime)
    assert runtime.state == "running"


async def test_starting_the_hub_is_refused_while_a_search_is_in_progress(paths, monkeypatch):
    """The reverse of the auto-yield: a manual Start mid-search would fight the search for the radio."""
    runtime = make_runtime(paths, source="none")
    runtime.discovery.status.state = "running"

    started = await runtime.start()

    assert started is False
    assert runtime.state == "failed"
    assert runtime.service is None
