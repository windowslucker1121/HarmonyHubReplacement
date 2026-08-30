"""Deployment settings: validation, persistence, and what counts as fatal.

The distinction these cover is the one the whole settings page rests on: a
value that is *wrong* is rejected, but a value that is merely *missing* is
saveable. You have to be able to write down "source: radio" before you have
gone and found the address.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from harmony_hub import settings as settings_module
from harmony_hub import storage
from harmony_hub.settings import HubSettings


def test_settings_round_trip_through_a_file(tmp_path):
    path = tmp_path / "hub_settings.json"
    original = HubSettings(port=9000, source="replay", replay_path=tmp_path / "cap.jsonl", address="17129bfcb6")

    settings_module.save(original, path)
    loaded, error = settings_module.load(path)

    assert error is None
    assert loaded == original


def test_an_unknown_key_is_rejected_rather_than_ignored():
    """A typo in a hand-edited settings file must not silently do nothing."""
    with pytest.raises(ValidationError):
        HubSettings.model_validate({"prot": 8765})


def test_an_address_is_normalised_to_upper_case_hex():
    assert HubSettings(address="17129bfcb6").address == "17129BFCB6"


def test_a_cleared_address_reads_as_unset_rather_than_invalid():
    """The settings form submits a cleared field as an empty string."""
    assert HubSettings(address="").address is None
    assert HubSettings(address="   ").address is None


@pytest.mark.parametrize("bad", ["17129BFCB", "17129BFCB6A", "ZZZZZZZZZZ"])
def test_a_malformed_address_is_a_hard_error(bad):
    with pytest.raises(ValidationError):
        HubSettings(address=bad)


def test_a_channel_outside_the_harmony_set_is_rejected():
    HubSettings(channel=62)  # one of the twelve
    with pytest.raises(ValidationError):
        HubSettings(channel=63)


def test_a_port_outside_the_valid_range_is_rejected():
    with pytest.raises(ValidationError):
        HubSettings(port=70000)


# ----------------------------------------------------------------------
# GitHub release checking
# ----------------------------------------------------------------------


def test_github_updates_default_on_with_a_sensible_repo():
    settings = HubSettings()
    assert settings.github_updates_enabled is True
    assert settings.github_repo == "windowslucker1121/HarmonyHubReplacement"
    assert settings.update_check_interval_hours == 6.0


def test_github_repo_is_normalised_and_validated():
    assert HubSettings(github_repo=" owner/name ").github_repo == "owner/name"
    for bad in ["", "no-slash", "/name", "owner/", "owner/name/extra"]:
        with pytest.raises(ValidationError):
            HubSettings(github_repo=bad)


def test_update_check_interval_zero_is_allowed_and_means_manual_only():
    assert HubSettings(update_check_interval_hours=0).update_check_interval_hours == 0


def test_update_check_interval_rejects_negative_or_absurdly_large_values():
    with pytest.raises(ValidationError):
        HubSettings(update_check_interval_hours=-1)
    with pytest.raises(ValidationError):
        HubSettings(update_check_interval_hours=1000)


# ----------------------------------------------------------------------
# Infrared pins -- one receiver and one transmitter, wired once
# ----------------------------------------------------------------------


def test_ir_pins_default_to_unwired():
    settings = HubSettings()
    assert settings.ir_rx_pin is None
    assert settings.ir_tx_pin is None


def test_an_ir_pin_outside_the_bcm_range_is_rejected():
    with pytest.raises(ValidationError):
        HubSettings(ir_rx_pin=28)
    with pytest.raises(ValidationError):
        HubSettings(ir_tx_pin=-1)


# ----------------------------------------------------------------------
# MQTT bridge (Home Assistant)
# ----------------------------------------------------------------------


def test_mqtt_is_off_by_default_with_no_broker_configured():
    settings = HubSettings()
    assert settings.mqtt_enabled is False
    assert settings.mqtt_host == ""
    assert settings.mqtt_node_id == "harmony_hub"


def test_mqtt_node_id_rejects_anything_that_would_break_a_topic():
    with pytest.raises(ValidationError):
        HubSettings(mqtt_node_id="not a topic segment")
    with pytest.raises(ValidationError):
        HubSettings(mqtt_node_id="Has/Slash")


def test_mqtt_node_id_accepts_lowercase_digits_underscore_and_hyphen():
    assert HubSettings(mqtt_node_id="living-room_hub2").mqtt_node_id == "living-room_hub2"


def test_mqtt_discovery_prefix_strips_slashes():
    assert HubSettings(mqtt_discovery_prefix="/homeassistant/").mqtt_discovery_prefix == "homeassistant"


def test_mqtt_settings_do_not_need_a_process_restart():
    """The bridge reconnects live -- see `HubRuntime.apply_settings`'s `mqtt_changed`."""
    live = HubSettings()
    assert not live.model_copy(update={"mqtt_enabled": True}).needs_process_restart(live)
    assert not live.model_copy(update={"mqtt_host": "broker.local"}).needs_process_restart(live)


def test_mqtt_settings_are_not_among_the_hub_start_problems():
    """The bridge degrades on its own; a bad broker must not block the hub itself from starting."""
    settings = HubSettings(mqtt_enabled=True, mqtt_host="")
    assert settings.problems() == []


def test_radio_gpio_is_empty_off_linux(monkeypatch):
    monkeypatch.setattr(settings_module.platform, "system", lambda: "Windows")
    settings = HubSettings(csn_pin="D5", ce_pin="D6")
    assert settings.radio_gpio() == set()


def test_radio_gpio_reads_the_pi_pin_names_on_linux(monkeypatch):
    monkeypatch.setattr(settings_module.platform, "system", lambda: "Linux")
    settings = HubSettings(csn_pin="D5", ce_pin="D6")
    assert settings.radio_gpio() == {5, 6}


def test_radio_gpio_ignores_ft232h_style_names_even_on_linux(monkeypatch):
    """`C0` addresses the FT232H breakout, not a BCM pin -- even under an
    FT232H dev setup running on Linux, this must not report a phantom
    collision on GPIO0.
    """
    monkeypatch.setattr(settings_module.platform, "system", lambda: "Linux")
    settings = HubSettings(csn_pin="C0", ce_pin="D4")
    assert settings.radio_gpio() == {4}


# ----------------------------------------------------------------------
# Advisory problems -- saveable, but the hub will not start on them
# ----------------------------------------------------------------------


def test_radio_without_an_address_is_saveable_but_reported():
    settings = HubSettings(source="radio")

    assert settings.problems() == ["Source is 'radio' but no remote address is set."]


def test_replay_pointing_at_a_missing_file_is_reported(tmp_path):
    settings = HubSettings(source="replay", replay_path=tmp_path / "gone.jsonl")

    assert "does not exist" in settings.problems()[0]


def test_a_complete_configuration_has_no_problems(tmp_path):
    capture = tmp_path / "cap.jsonl"
    capture.write_text("", encoding="utf-8")

    assert HubSettings(source="replay", replay_path=capture).problems() == []
    assert HubSettings(source="radio", address="17129BFCB6").problems() == []
    assert HubSettings(source="none").problems() == []


# ----------------------------------------------------------------------
# Loading must never be fatal
# ----------------------------------------------------------------------


def test_a_missing_file_gives_defaults_not_an_error(tmp_path):
    settings, error = settings_module.load(tmp_path / "nothing.json")

    assert error is None
    assert settings == HubSettings()


def test_a_corrupt_file_falls_back_to_defaults_instead_of_raising(tmp_path):
    """A settings file with a typo must not stop the web server.

    The web server is where the typo gets fixed, so taking it down over one
    is the one failure mode this whole design exists to remove.
    """
    path = tmp_path / "hub_settings.json"
    path.write_text("{not json at all", encoding="utf-8")

    settings, error = settings_module.load(path)

    assert settings == HubSettings()
    assert error is not None and "could not be read" in error


def test_a_file_with_an_unknown_key_also_falls_back(tmp_path):
    path = tmp_path / "hub_settings.json"
    path.write_text(json.dumps({"port": 9000, "nonsense": True}), encoding="utf-8")

    settings, error = settings_module.load(path)

    assert settings.port == 8765  # the default, not the file's 9000
    assert error is not None


def test_a_failed_save_does_not_destroy_the_previous_settings(tmp_path, monkeypatch):
    path = tmp_path / "hub_settings.json"
    settings_module.save(HubSettings(port=9000), path)
    before = path.read_text()

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage.os, "replace", explode)
    with pytest.raises(OSError):
        settings_module.save(HubSettings(port=1234), path)

    assert path.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["hub_settings.json"]


# ----------------------------------------------------------------------


def test_only_bind_settings_need_a_process_restart():
    live = HubSettings()

    assert live.model_copy(update={"port": 9000}).needs_process_restart(live)
    assert live.model_copy(update={"host": "127.0.0.1"}).needs_process_restart(live)
    # Everything else applies by restarting the hub, which the page survives.
    assert not live.model_copy(update={"source": "radio"}).needs_process_restart(live)
    assert not live.model_copy(update={"address": "17129BFCB6"}).needs_process_restart(live)
    assert not live.model_copy(update={"github_updates_enabled": False}).needs_process_restart(live)
    assert not live.model_copy(update={"update_check_interval_hours": 1.0}).needs_process_restart(live)


# ----------------------------------------------------------------------
# Command-line overrides
# ----------------------------------------------------------------------


def _overridden(saved: HubSettings, argv: list[str]) -> HubSettings:
    from harmony_hub.server import apply_overrides, build_parser

    return apply_overrides(saved, build_parser().parse_args(argv))


def test_a_flag_that_was_not_given_leaves_the_saved_value_alone():
    """Every parser default is None for this reason.

    An unspecified `--port` must not quietly reset a saved 9000 back to 8765.
    """
    saved = HubSettings(port=9000, source="replay", replay_speed=3.0)

    result = _overridden(saved, [])

    assert result.port == 9000
    assert result.source == "replay"
    assert result.replay_speed == 3.0


def test_a_flag_that_was_given_wins_for_this_run():
    result = _overridden(HubSettings(port=9000), ["--port", "1234", "--source", "radio"])

    assert result.port == 1234
    assert result.source == "radio"


def test_store_true_flags_only_ever_turn_something_on():
    """They cannot be told apart from "not given", so they must not turn things off."""
    saved = HubSettings(allow_ack=True, autostart=True)

    assert _overridden(saved, []).allow_ack is True
    assert _overridden(saved, []).autostart is True
    assert _overridden(saved, ["--no-autostart"]).autostart is False


def test_an_override_is_validated_rather_than_stored_blindly():
    """`model_copy(update=...)` would skip validators; a bad address must fail here."""
    with pytest.raises(ValidationError):
        _overridden(HubSettings(), ["--address", "not-hex"])


def test_a_path_override_arrives_as_a_path(tmp_path):
    result = _overridden(HubSettings(), ["--config", str(tmp_path / "other.json")])

    assert result.config_path == tmp_path / "other.json"


def test_the_source_describes_itself_for_the_settings_page(tmp_path):
    assert HubSettings().describe_source() == "Simulated presses only"
    assert "17129BFCB6" in HubSettings(source="radio", address="17129BFCB6").describe_source()
    assert "no address" in HubSettings(source="radio").describe_source()
    assert "cap.jsonl" in HubSettings(source="replay", replay_path=tmp_path / "cap.jsonl").describe_source()
