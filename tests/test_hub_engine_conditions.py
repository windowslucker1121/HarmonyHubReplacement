"""`if`, `set`, `wait_for` and the value/condition machinery behind them.

Driven entirely by the virtual backend's `state`/`state_effects`/`unreadable`
config -- see `backends.virtual.VirtualBackend` -- so a condition's answer,
and how it changes once a command runs, are both controllable without real
equipment. No radio, no network.
"""

from __future__ import annotations

import asyncio

import pytest

from harmony_hub.engine import SceneEngine
from harmony_hub.models import HubConfig
from harmony_receiver.profiles import ButtonMap

TEST_COMMANDS = ["power_on", "power_off", "set_input"]


@pytest.fixture
def buttons() -> ButtonMap:
    return ButtonMap()


def tv_device(**config_overrides) -> dict:
    config = {"commands": TEST_COMMANDS, "state": {"power": "standby"}, **config_overrides}
    return {"id": "tv", "name": "TV", "backend": "virtual", "config": config}


async def make_engine(devices: list[dict], scenes: list[dict], buttons: ButtonMap) -> SceneEngine:
    engine = SceneEngine(
        HubConfig.model_validate({"devices": devices, "scenes": scenes, "global_scene": None}), buttons
    )
    await engine.start()
    return engine


def calls(engine: SceneEngine, device_id: str = "tv") -> list[str]:
    return [call["command"] for call in engine.backend_for(device_id).calls]


def scene_with(on_start: list[dict]) -> dict:
    return {"id": "s", "name": "S", "devices": ["tv"], "on_start": on_start}


# --------------------------------------------------------------------------
# if
# --------------------------------------------------------------------------


async def test_if_runs_then_when_the_condition_holds(buttons):
    engine = await make_engine(
        [tv_device(state={"power": "standby"})],
        [
            scene_with(
                [
                    {
                        "type": "if",
                        "condition": {
                            "left": {"type": "state", "device": "tv", "target": "power"},
                            "op": "is",
                            "right": {"type": "literal", "value": "standby"},
                        },
                        "then": [{"type": "device", "device": "tv", "command": "power_on"}],
                        "otherwise": [{"type": "device", "device": "tv", "command": "power_off"}],
                    }
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["power_on"]


async def test_if_runs_otherwise_when_the_condition_fails(buttons):
    engine = await make_engine(
        [tv_device(state={"power": "on"})],
        [
            scene_with(
                [
                    {
                        "type": "if",
                        "condition": {
                            "left": {"type": "state", "device": "tv", "target": "power"},
                            "op": "is",
                            "right": {"type": "literal", "value": "standby"},
                        },
                        "then": [{"type": "device", "device": "tv", "command": "power_on"}],
                        "otherwise": [{"type": "device", "device": "tv", "command": "power_off"}],
                    }
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["power_off"]


async def test_a_command_inside_then_can_change_the_state_a_later_condition_reads(buttons):
    """The TV example from the feature request: turn it on if it was off,
    wait for it to report on, then pick the input -- three steps that only
    make sense in order, run from one `if`."""
    engine = await make_engine(
        [tv_device(state={"power": "standby"}, state_effects={"power_on": {"power": "on"}})],
        [
            scene_with(
                [
                    {
                        "type": "if",
                        "condition": {
                            "left": {"type": "state", "device": "tv", "target": "power"},
                            "op": "is",
                            "right": {"type": "literal", "value": "standby"},
                        },
                        "then": [
                            {"type": "device", "device": "tv", "command": "power_on"},
                            {
                                "type": "wait_for",
                                "condition": {
                                    "left": {"type": "state", "device": "tv", "target": "power"},
                                    "op": "is",
                                    "right": {"type": "literal", "value": "on"},
                                },
                                "timeout": 2,
                                "poll": 0.05,
                            },
                            {"type": "device", "device": "tv", "command": "set_input"},
                        ],
                        "otherwise": [],
                    }
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["power_on", "set_input"]


async def test_an_if_can_nest_inside_another_ifs_branch(buttons):
    engine = await make_engine(
        [tv_device(state={"power": "on"})],
        [
            scene_with(
                [
                    {
                        "type": "if",
                        "condition": {
                            "left": {"type": "state", "device": "tv", "target": "power"},
                            "op": "is",
                            "right": {"type": "literal", "value": "standby"},
                        },
                        "then": [{"type": "device", "device": "tv", "command": "power_off"}],
                        "otherwise": [
                            {
                                "type": "if",
                                "condition": {
                                    "left": {"type": "state", "device": "tv", "target": "power"},
                                    "op": "is",
                                    "right": {"type": "literal", "value": "on"},
                                },
                                "then": [{"type": "device", "device": "tv", "command": "set_input"}],
                                "otherwise": [],
                            }
                        ],
                    }
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["set_input"]


# --------------------------------------------------------------------------
# on_unreadable
# --------------------------------------------------------------------------


async def test_an_unreadable_condition_runs_then_by_default(buttons):
    engine = await make_engine(
        [tv_device(unreadable=["power"])],
        [
            scene_with(
                [
                    {
                        "type": "if",
                        "condition": {
                            "left": {"type": "state", "device": "tv", "target": "power"},
                            "op": "is",
                            "right": {"type": "literal", "value": "standby"},
                        },
                        "then": [{"type": "device", "device": "tv", "command": "power_on"}],
                        "otherwise": [{"type": "device", "device": "tv", "command": "power_off"}],
                    }
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["power_on"]


async def test_on_unreadable_skip_runs_otherwise_instead(buttons):
    engine = await make_engine(
        [tv_device(unreadable=["power"])],
        [
            scene_with(
                [
                    {
                        "type": "if",
                        "condition": {
                            "left": {"type": "state", "device": "tv", "target": "power"},
                            "op": "is",
                            "right": {"type": "literal", "value": "standby"},
                            "on_unreadable": "skip",
                        },
                        "then": [{"type": "device", "device": "tv", "command": "power_on"}],
                        "otherwise": [{"type": "device", "device": "tv", "command": "power_off"}],
                    }
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["power_off"]


async def test_a_target_never_seeded_into_state_is_unreadable(buttons):
    """A target that is not one of the device's known state keys fails the
    same way an offline device does -- `read_state` raises `BackendError`
    either way, which is what stands in for a device that is not `Readable`
    at all (an IR device has no `read_state` to call in the first place, so
    the same `isinstance` check in `_read_device_state` catches that case
    too; there is no virtual stand-in for a missing method).
    """
    engine = await make_engine(
        [tv_device(state={})],
        [
            scene_with(
                [
                    {
                        "type": "if",
                        "condition": {"left": {"type": "state", "device": "tv", "target": "power"}, "op": "known"},
                        "then": [{"type": "device", "device": "tv", "command": "power_on"}],
                        "otherwise": [{"type": "device", "device": "tv", "command": "power_off"}],
                    }
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["power_off"]


# --------------------------------------------------------------------------
# known / unknown
# --------------------------------------------------------------------------


async def test_known_is_true_when_the_state_can_be_read(buttons):
    engine = await make_engine(
        [tv_device(state={"power": "on"})],
        [
            scene_with(
                [
                    {
                        "type": "if",
                        "condition": {"left": {"type": "state", "device": "tv", "target": "power"}, "op": "known"},
                        "then": [{"type": "device", "device": "tv", "command": "power_on"}],
                        "otherwise": [],
                    }
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["power_on"]


async def test_unknown_is_true_when_the_state_cannot_be_read(buttons):
    engine = await make_engine(
        [tv_device(unreadable=["power"])],
        [
            scene_with(
                [
                    {
                        "type": "if",
                        "condition": {"left": {"type": "state", "device": "tv", "target": "power"}, "op": "unknown"},
                        "then": [{"type": "device", "device": "tv", "command": "power_on"}],
                        "otherwise": [],
                    }
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["power_on"]


# --------------------------------------------------------------------------
# set / var
# --------------------------------------------------------------------------


async def test_set_stores_a_devices_state_for_later_recall(buttons):
    engine = await make_engine(
        [tv_device(state={"source": "hdmi1"})],
        [scene_with([{"type": "set", "name": "prev", "value": {"type": "state", "device": "tv", "target": "source"}}])],
        buttons,
    )
    await engine.activate_scene("s")
    assert engine.variables == {"prev": "hdmi1"}


async def test_a_device_action_param_can_restore_a_stored_variable(buttons):
    engine = await make_engine(
        [tv_device(state={"source": "hdmi1"})],
        [
            scene_with(
                [
                    {"type": "set", "name": "prev", "value": {"type": "state", "device": "tv", "target": "source"}},
                    {
                        "type": "device",
                        "device": "tv",
                        "command": "set_input",
                        "params": {"source": {"type": "var", "name": "prev"}},
                    },
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    backend = engine.backend_for("tv")
    assert backend.calls[-1]["command"] == "set_input"
    assert backend.calls[-1]["params"] == {"source": "hdmi1"}


async def test_reading_an_unset_variable_is_unreadable(buttons):
    engine = await make_engine(
        [tv_device()],
        [
            scene_with(
                [
                    {
                        "type": "if",
                        "condition": {"left": {"type": "var", "name": "never_set"}, "op": "unknown"},
                        "then": [{"type": "device", "device": "tv", "command": "power_on"}],
                        "otherwise": [],
                    }
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["power_on"]


async def test_variables_survive_a_reload_but_not_a_stop(buttons):
    engine = await make_engine(
        [tv_device(state={"source": "hdmi1"})],
        [scene_with([{"type": "set", "name": "prev", "value": {"type": "state", "device": "tv", "target": "source"}}])],
        buttons,
    )
    await engine.activate_scene("s")
    assert engine.variables == {"prev": "hdmi1"}

    await engine.reload(engine.config)
    assert engine.variables == {"prev": "hdmi1"}

    await engine.stop()
    assert engine.variables == {}


# --------------------------------------------------------------------------
# wait_for
# --------------------------------------------------------------------------


async def test_wait_for_returns_immediately_once_the_condition_already_holds(buttons):
    engine = await make_engine(
        [tv_device(state={"power": "on"})],
        [
            scene_with(
                [
                    {
                        "type": "wait_for",
                        "condition": {
                            "left": {"type": "state", "device": "tv", "target": "power"},
                            "op": "is",
                            "right": {"type": "literal", "value": "on"},
                        },
                        "timeout": 5,
                    },
                    {"type": "device", "device": "tv", "command": "set_input"},
                ]
            )
        ],
        buttons,
    )
    started = asyncio.get_event_loop().time()
    await engine.activate_scene("s")
    elapsed = asyncio.get_event_loop().time() - started
    assert calls(engine) == ["set_input"]
    assert elapsed < 1.0


async def test_wait_for_polls_until_a_later_command_flips_the_state(buttons):
    """No command in this macro flips `power` -- it is flipped from outside,
    simulating the device coming on on its own schedule -- so this only
    passes if `wait_for` genuinely polls rather than reading once.
    """
    engine = await make_engine(
        [tv_device(state={"power": "standby"})],
        [
            scene_with(
                [
                    {
                        "type": "wait_for",
                        "condition": {
                            "left": {"type": "state", "device": "tv", "target": "power"},
                            "op": "is",
                            "right": {"type": "literal", "value": "on"},
                        },
                        "timeout": 3,
                        "poll": 0.05,
                    },
                    {"type": "device", "device": "tv", "command": "set_input"},
                ]
            )
        ],
        buttons,
    )

    async def flip_after_a_moment():
        await asyncio.sleep(0.15)
        engine.backend_for("tv")._state["power"] = "on"

    await asyncio.gather(engine.activate_scene("s"), flip_after_a_moment())
    assert calls(engine) == ["set_input"]


async def test_wait_for_on_timeout_continue_still_runs_what_follows(buttons):
    engine = await make_engine(
        [tv_device(state={"power": "standby"})],
        [
            scene_with(
                [
                    {
                        "type": "wait_for",
                        "condition": {
                            "left": {"type": "state", "device": "tv", "target": "power"},
                            "op": "is",
                            "right": {"type": "literal", "value": "on"},
                        },
                        "timeout": 0.1,
                        "poll": 0.03,
                        "on_timeout": "continue",
                    },
                    {"type": "device", "device": "tv", "command": "set_input"},
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["set_input"]


async def test_wait_for_on_timeout_stop_still_runs_the_next_sibling_action(buttons):
    """`run_actions` never lets one action's failure cancel its siblings --
    `on_timeout: stop` only changes how the timeout is logged (as a failure)
    rather than whether the macro keeps going.
    """
    engine = await make_engine(
        [tv_device(state={"power": "standby"})],
        [
            scene_with(
                [
                    {
                        "type": "wait_for",
                        "condition": {
                            "left": {"type": "state", "device": "tv", "target": "power"},
                            "op": "is",
                            "right": {"type": "literal", "value": "on"},
                        },
                        "timeout": 0.1,
                        "poll": 0.03,
                        "on_timeout": "stop",
                    },
                    {"type": "device", "device": "tv", "command": "set_input"},
                ]
            )
        ],
        buttons,
    )
    await engine.activate_scene("s")
    assert calls(engine) == ["set_input"]


# --------------------------------------------------------------------------
# Power-filter recursion (`_start_actions` / `_stop_actions`)
# --------------------------------------------------------------------------


async def test_a_power_off_inside_an_if_branch_is_still_skipped_for_a_shared_device(buttons):
    devices = [tv_device(), {"id": "amp", "name": "Amp", "backend": "virtual", "config": {"commands": TEST_COMMANDS}}]
    scenes = [
        {
            "id": "a",
            "name": "A",
            "devices": ["tv", "amp"],
            "on_stop": [
                {
                    "type": "if",
                    "condition": {
                        "left": {"type": "literal", "value": "x"},
                        "op": "is",
                        "right": {"type": "literal", "value": "x"},
                    },
                    "then": [{"type": "device", "device": "tv", "command": "power_off"}],
                    "otherwise": [],
                }
            ],
        },
        {"id": "b", "name": "B", "devices": ["tv"]},
    ]
    engine = await make_engine(devices, scenes, buttons)
    await engine.activate_scene("a")
    await engine.activate_scene("b")
    # "b" still needs the TV, so the power-off buried inside the `if` must be
    # skipped exactly as an un-nested one would be.
    assert "power_off" not in calls(engine)


async def test_an_if_left_with_both_branches_empty_after_filtering_is_dropped(buttons):
    """Both branches power off a device the next scene still needs, so both
    are filtered to nothing -- the `if` itself should vanish rather than
    evaluate a condition for no reason.
    """
    devices = [tv_device()]
    scenes = [
        {
            "id": "a",
            "name": "A",
            "devices": ["tv"],
            "on_stop": [
                {
                    "type": "if",
                    "condition": {
                        "left": {"type": "state", "device": "tv", "target": "power"},
                        "op": "known",
                    },
                    "then": [{"type": "device", "device": "tv", "command": "power_off"}],
                    "otherwise": [{"type": "device", "device": "tv", "command": "power_off"}],
                }
            ],
        },
        {"id": "b", "name": "B", "devices": ["tv"]},
    ]
    engine = await make_engine(devices, scenes, buttons)
    await engine.activate_scene("a")
    await engine.activate_scene("b")
    assert calls(engine) == []
