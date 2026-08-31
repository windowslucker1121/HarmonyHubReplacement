"""Configuration model validation and round-tripping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harmony_hub import config as config_module
from harmony_hub import storage
from harmony_hub.models import (
    AdjustAction,
    Binding,
    Device,
    DeviceAction,
    HubConfig,
    Scene,
    SceneAction,
    TransitionValue,
)


def make_config(**overrides) -> dict:
    base = {
        "devices": [{"id": "tv", "name": "TV", "backend": "virtual"}],
        "scenes": [
            {
                "id": "watch_tv",
                "name": "Watch TV",
                "devices": ["tv"],
                "on_start": [{"type": "device", "device": "tv", "command": "power_on"}],
                "bindings": {
                    "volume_up": {"on_press": [{"type": "device", "device": "tv", "command": "volume_up"}]}
                },
            }
        ],
    }
    base.update(overrides)
    return base


def test_a_valid_config_loads():
    config = HubConfig.model_validate(make_config())

    assert config.device("tv").name == "TV"
    assert config.scene("watch_tv").bindings["volume_up"].on_press[0].command == "volume_up"


def test_actions_are_discriminated_by_type():
    broken = make_config()
    broken["scenes"][0]["bindings"]["play"] = {
        "on_press": [
            {"type": "scene", "scene": "watch_tv"},
            {"type": "delay", "seconds": 1.5},
            {"type": "device", "device": "tv", "command": "play"},
        ]
    }
    config = HubConfig.model_validate(broken)
    actions = config.scene("watch_tv").bindings["play"].on_press

    assert isinstance(actions[0], SceneAction)
    assert actions[1].seconds == 1.5
    assert isinstance(actions[2], DeviceAction)


def test_an_action_pointing_at_a_missing_device_is_rejected():
    """Otherwise the typo only surfaces when someone presses that button."""
    broken = make_config()
    broken["scenes"][0]["bindings"]["volume_up"]["on_press"][0]["device"] = "nope"

    with pytest.raises(ValidationError, match="unknown device 'nope'"):
        HubConfig.model_validate(broken)


def test_an_action_pointing_at_a_missing_scene_is_rejected():
    broken = make_config()
    broken["scenes"][0]["bindings"]["play"] = {"on_press": [{"type": "scene", "scene": "ghost"}]}

    with pytest.raises(ValidationError, match="unknown scene 'ghost'"):
        HubConfig.model_validate(broken)


def test_an_adjust_action_round_trips_with_no_fallback():
    config = make_config()
    config["scenes"][0]["bindings"]["ff0"] = {"on_press": [{"type": "adjust", "direction": "up"}]}
    loaded = HubConfig.model_validate(config)

    action = loaded.scene("watch_tv").bindings["ff0"].on_press[0]
    assert isinstance(action, AdjustAction)
    assert action.direction == "up"
    assert action.device is None
    assert action.target is None


def test_an_adjust_action_can_carry_a_fallback_device_and_target():
    config = make_config()
    config["scenes"][0]["bindings"]["ff0"] = {
        "on_press": [{"type": "adjust", "direction": "down", "device": "tv", "target": "light.kitchen"}]
    }
    loaded = HubConfig.model_validate(config)

    action = loaded.scene("watch_tv").bindings["ff0"].on_press[0]
    assert action.device == "tv"
    assert action.target == "light.kitchen"


def test_an_adjust_action_rejects_a_bad_direction():
    config = make_config()
    config["scenes"][0]["bindings"]["ff0"] = {"on_press": [{"type": "adjust", "direction": "sideways"}]}

    with pytest.raises(ValidationError):
        HubConfig.model_validate(config)


def test_an_adjust_actions_fallback_target_needs_a_device():
    """A target naming an entity means nothing without a device to resolve it against."""
    config = make_config()
    config["scenes"][0]["bindings"]["ff0"] = {
        "on_press": [{"type": "adjust", "direction": "up", "target": "light.kitchen"}]
    }

    with pytest.raises(ValidationError, match="means nothing without 'device'"):
        HubConfig.model_validate(config)


def test_an_adjust_actions_fallback_device_must_exist():
    config = make_config()
    config["scenes"][0]["bindings"]["ff0"] = {
        "on_press": [{"type": "adjust", "direction": "up", "device": "ghost", "target": "light.kitchen"}]
    }

    with pytest.raises(ValidationError, match="unknown device 'ghost'"):
        HubConfig.model_validate(config)


def test_a_scene_listing_a_missing_device_is_rejected():
    broken = make_config()
    broken["scenes"][0]["devices"] = ["tv", "ghost"]

    with pytest.raises(ValidationError, match="unknown device 'ghost'"):
        HubConfig.model_validate(broken)


def test_a_missing_default_scene_is_rejected():
    with pytest.raises(ValidationError, match="default_scene"):
        HubConfig.model_validate(make_config(default_scene="nope"))


def test_a_missing_global_scene_is_rejected():
    with pytest.raises(ValidationError, match="global_scene"):
        HubConfig.model_validate(make_config(global_scene="nope"))


def test_a_global_scene_can_reference_any_configured_scene():
    config = HubConfig.model_validate(make_config(global_scene="watch_tv"))

    assert config.global_scene == "watch_tv"


def test_duplicate_ids_are_rejected():
    broken = make_config()
    broken["devices"].append({"id": "tv", "name": "Other TV", "backend": "virtual"})

    with pytest.raises(ValidationError, match="unique"):
        HubConfig.model_validate(broken)


def test_a_scene_action_with_no_scene_means_stop():
    """`{"type": "scene"}` is the Off button, and must not fail validation."""
    broken = make_config()
    broken["scenes"][0]["bindings"]["off"] = {"on_press": [{"type": "scene"}]}
    config = HubConfig.model_validate(broken)

    assert config.scene("watch_tv").bindings["off"].on_press[0].scene is None


def _if_on(condition: dict) -> dict:
    return {"type": "if", "condition": condition, "then": [], "otherwise": []}


def test_a_transition_value_round_trips():
    config = make_config()
    config["scenes"][0]["on_start"].append(
        _if_on({"left": {"type": "transition", "edge": "from"}, "op": "known"})
    )
    loaded = HubConfig.model_validate(config)

    action = loaded.scene("watch_tv").on_start[-1]
    assert isinstance(action.condition.left, TransitionValue)
    assert action.condition.left.edge == "from"


def test_a_transition_compared_against_an_unknown_scene_is_rejected():
    config = make_config()
    config["scenes"][0]["on_start"].append(
        _if_on(
            {
                "left": {"type": "transition", "edge": "from"},
                "op": "is",
                "right": {"type": "literal", "value": "ghost"},
            }
        )
    )

    with pytest.raises(ValidationError, match="unknown scene 'ghost'"):
        HubConfig.model_validate(config)


def test_a_transition_compared_against_an_empty_literal_is_not_a_scene_reference():
    """An empty literal means idle, not a scene named the empty string --
    the one literal value a transition condition never has to name a real
    scene to use."""
    config = make_config()
    config["scenes"][0]["on_start"].append(
        _if_on(
            {
                "left": {"type": "transition", "edge": "from"},
                "op": "is",
                "right": {"type": "literal", "value": ""},
            }
        )
    )

    HubConfig.model_validate(config)  # does not raise


def test_a_transition_compared_against_a_real_scene_is_accepted():
    config = make_config(scenes=[*make_config()["scenes"], {"id": "music", "name": "Music"}])
    config["scenes"][0]["on_start"].append(
        _if_on(
            {
                "left": {"type": "transition", "edge": "to"},
                "op": "is_not",
                "right": {"type": "literal", "value": "music"},
            }
        )
    )

    HubConfig.model_validate(config)  # does not raise


def test_an_ordinary_literal_condition_does_not_need_to_be_a_scene():
    """Only a literal actually compared against a `transition` value is held
    to "must be a real scene id" -- an ordinary condition's literal means
    whatever it says.
    """
    config = make_config()
    config["scenes"][0]["on_start"].append(
        _if_on(
            {
                "left": {"type": "literal", "value": "anything"},
                "op": "is",
                "right": {"type": "literal", "value": "not-a-scene-id either"},
            }
        )
    )

    HubConfig.model_validate(config)  # does not raise


def test_unknown_keys_are_rejected():
    """A typo in hand-edited config should be an error, not a silent no-op."""
    with pytest.raises(ValidationError):
        Device.model_validate({"id": "tv", "name": "TV", "backend": "virtual", "powerpolicy": "managed"})


def test_device_ids_must_be_slugs():
    with pytest.raises(ValidationError):
        Device.model_validate({"id": "Living Room", "name": "TV", "backend": "virtual"})


def test_an_empty_config_is_valid():
    """First run: the UI is how the first device gets added."""
    assert HubConfig().devices == []


def test_binding_reports_emptiness_and_looks_up_phases():
    binding = Binding(on_press=[SceneAction(scene=None)])

    assert not binding.is_empty
    assert len(binding.actions_for("press")) == 1
    assert binding.actions_for("release") == []
    assert Binding().is_empty


def test_repeat_acceleration_defaults_to_off():
    """`repeat_accel` at 1.0 is what makes acceleration opt-in: an existing
    config that has never heard of it must load and behave unchanged."""
    binding = Binding()
    config = HubConfig.model_validate(make_config())

    assert binding.repeat_accel is None
    assert binding.repeat_accel_seconds is None
    assert config.default_repeat_accel == 1.0
    assert config.default_repeat_accel_seconds == 3.0


def test_repeat_acceleration_round_trips_on_a_binding_and_the_config():
    config = HubConfig.model_validate(make_config(
        default_repeat_accel=4,
        default_repeat_accel_seconds=2,
        scenes=[
            {
                "id": "watch_tv",
                "name": "Watch TV",
                "devices": ["tv"],
                "bindings": {
                    "volume_up": {
                        "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
                        "repeat_accel": 8,
                        "repeat_accel_seconds": 1.5,
                    }
                },
            }
        ],
    ))

    assert config.default_repeat_accel == 4
    assert config.default_repeat_accel_seconds == 2
    binding = config.scene("watch_tv").bindings["volume_up"]
    assert binding.repeat_accel == 8
    assert binding.repeat_accel_seconds == 1.5


def test_config_round_trips_through_disk(tmp_path):
    original = HubConfig.model_validate(make_config())
    path = tmp_path / "hub_config.json"

    config_module.save(original, path)

    assert config_module.load(path) == original


def test_loading_a_missing_file_returns_an_empty_config(tmp_path):
    assert config_module.load(tmp_path / "absent.json") == HubConfig()


def test_saving_leaves_no_temporary_files_behind(tmp_path):
    path = tmp_path / "hub_config.json"

    config_module.save(HubConfig.model_validate(make_config()), path)

    assert [p.name for p in tmp_path.iterdir()] == ["hub_config.json"]


def test_a_failed_save_does_not_destroy_the_previous_config(tmp_path, monkeypatch):
    """An atomic write is the whole point: a crash must not truncate the file."""
    path = tmp_path / "hub_config.json"
    config_module.save(HubConfig.model_validate(make_config()), path)
    before = path.read_text()

    def explode(*args, **kwargs):
        raise OSError("disk full")

    # The atomic write lives in `storage` now, shared with the settings file.
    monkeypatch.setattr(storage.os, "replace", explode)
    with pytest.raises(OSError):
        config_module.save(HubConfig(), path)

    assert path.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["hub_config.json"]


def test_scene_and_device_lookup_return_none_when_absent():
    config = HubConfig.model_validate(make_config())

    assert config.device("ghost") is None
    assert config.scene("ghost") is None


def test_scene_requires_a_name():
    with pytest.raises(ValidationError):
        Scene.model_validate({"id": "watch_tv"})
