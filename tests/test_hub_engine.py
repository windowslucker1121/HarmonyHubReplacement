"""SceneEngine behaviour, driven entirely by the virtual backend.

No radio, no network, no real equipment. Hold timings use deliberately tiny
windows so the suite stays fast while still exercising the real timing code
rather than a mocked clock.
"""

from __future__ import annotations

import asyncio

import pytest

from harmony_hub.engine import Focus, SceneEngine
from harmony_hub.models import HubConfig
from harmony_receiver.events import RemoteEvent
from harmony_receiver.profiles import ButtonMap

VOLUME_UP_SIGNATURE = "C3E90000"
POWER_SIGNATURE = "C3300000"


@pytest.fixture
def buttons() -> ButtonMap:
    buttons = ButtonMap()
    buttons.learn("volume_up", "Volume Up", VOLUME_UP_SIGNATURE)
    buttons.learn("power", "Power", POWER_SIGNATURE)
    return buttons


def press(signature: str = VOLUME_UP_SIGNATURE, kind: str = "press") -> RemoteEvent:
    return RemoteEvent(kind=kind, signature=signature)  # type: ignore[arg-type]


# The virtual backend rejects commands it was not told about, exactly as real
# equipment would, so the test devices have to declare what they accept.
TEST_COMMANDS = [
    "power_on", "power_off", "volume_up", "set_input", "play",
    "tap", "held", "ramp", "stop", "first", "boom", "third",
    "toggle", "brighter", "dimmer",
]


def config_dict(**overrides) -> dict:
    """Building block for engine tests.

    `global_bindings` is test-fixture sugar, not a real config field: it is
    turned into a scene called "idle" that `global_scene` points at, which is
    how a real configuration expresses a fallback -- a reference to an
    ordinary scene rather than a second, parallel set of bindings. Pass
    `global_bindings={}` (or your own `scenes`/`global_scene`) to opt out.
    """
    global_bindings = overrides.pop(
        "global_bindings",
        {"volume_up": {"on_press": [{"type": "device", "device": "tv", "command": "volume_up"}]}},
    )
    scenes = overrides.pop(
        "scenes",
        [
            {
                "id": "watch_tv",
                "name": "Watch TV",
                "devices": ["tv", "amp"],
                "on_start": [{"type": "device", "device": "tv", "command": "power_on"}],
                "on_stop": [{"type": "device", "device": "tv", "command": "power_off"}],
                "bindings": {
                    "volume_up": {"on_press": [{"type": "device", "device": "amp", "command": "volume_up"}]}
                },
            },
            {
                "id": "music",
                "name": "Music",
                "devices": ["amp"],
                "on_start": [{"type": "device", "device": "amp", "command": "power_on"}],
                "bindings": {},
            },
        ],
    )

    base = {
        "devices": [
            {"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": TEST_COMMANDS}},
            {"id": "amp", "name": "Amp", "backend": "virtual", "config": {"commands": TEST_COMMANDS}},
        ],
        "scenes": scenes,
        "global_scene": None,
    }
    if global_bindings:
        base["scenes"] = [*scenes, {"id": "idle", "name": "Idle", "bindings": global_bindings}]
        base["global_scene"] = "idle"
    base.update(overrides)
    return base


async def make_engine(buttons: ButtonMap, **overrides) -> SceneEngine:
    engine = SceneEngine(HubConfig.model_validate(config_dict(**overrides)), buttons)
    await engine.start()
    return engine


# "amp" claims the focus (target "lamp") when sent "toggle", and can be
# stepped up/down through "brighter"/"dimmer" -- just enough vocabulary for
# the virtual backend to stand in for a real one in the focus/adjust tests.
FOCUS_DEVICES = [
    {"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": TEST_COMMANDS}},
    {
        "id": "amp",
        "name": "Amp",
        "backend": "virtual",
        "config": {
            "commands": TEST_COMMANDS,
            "focus": {"toggle": ["lamp", "Lamp"]},
            "adjust": {"lamp": {"up": "brighter", "down": "dimmer"}},
        },
    },
]


async def make_focus_engine(buttons: ButtonMap, **binding_overrides) -> SceneEngine:
    """An engine with `FOCUS_DEVICES`, "volume_up" bound to the toggle that
    claims the focus, and "power" bound to an adjust-up action -- the shared
    setup every focus/adjust test starts from."""
    global_bindings = {
        "volume_up": {"on_press": [{"type": "device", "device": "amp", "command": "toggle"}]},
        "power": {"on_press": [{"type": "adjust", "direction": "up"}]},
    }
    global_bindings.update(binding_overrides)
    return await make_engine(buttons, devices=FOCUS_DEVICES, global_bindings=global_bindings)


def commands(engine: SceneEngine, device_id: str) -> list[str]:
    return [call["command"] for call in engine.backend_for(device_id).calls]


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


async def test_a_press_runs_the_bound_action(buttons):
    engine = await make_engine(buttons)

    await engine.handle(press())

    assert commands(engine, "tv") == ["volume_up"]


async def test_the_active_scene_overrides_the_global_binding(buttons):
    """The whole point of a scene: the same button reaches different equipment."""
    engine = await make_engine(buttons)

    await engine.handle(press())
    await engine.activate_scene("watch_tv")
    await engine.handle(press())

    assert commands(engine, "tv") == ["volume_up", "power_on"]
    assert commands(engine, "amp") == ["volume_up"]


async def test_a_scene_falls_back_to_global_bindings(buttons):
    """Music binds nothing, so volume must still work."""
    engine = await make_engine(buttons)
    await engine.activate_scene("music")

    await engine.handle(press())

    assert commands(engine, "tv") == ["volume_up"]


async def test_the_global_fallback_is_an_ordinary_scenes_bindings(buttons):
    """`global_scene` is a reference, not a second bindable set: pointing it
    at "watch_tv" makes that scene's own bindings the idle-state fallback,
    with no separate global configuration involved at all."""
    engine = await make_engine(buttons, global_scene="watch_tv", global_bindings={})

    await engine.handle(press())

    assert commands(engine, "amp") == ["volume_up"]


async def test_with_no_global_scene_an_unbound_button_does_nothing(buttons):
    engine = await make_engine(buttons, global_bindings={})

    await engine.handle(press())

    assert commands(engine, "tv") == []
    assert commands(engine, "amp") == []


async def test_a_scene_that_is_both_active_and_global_still_falls_back_to_itself(buttons):
    """Nothing should loop or crash when a scene is its own fallback -- it
    just means an unbound key stays unbound, exactly as if there were no
    fallback scene at all."""
    engine = await make_engine(buttons, global_scene="music", global_bindings={})
    await engine.activate_scene("music")  # runs its on_start: amp.power_on

    await engine.handle(press())  # volume_up is bound nowhere, including in "music" itself

    assert commands(engine, "tv") == []
    assert commands(engine, "amp") == ["power_on"]


async def test_an_unbound_button_does_nothing_but_is_reported(buttons):
    """An unmapped press must stay visible, or a new button can never be noticed."""
    engine = await make_engine(buttons)

    await engine.handle(press(POWER_SIGNATURE))

    assert commands(engine, "tv") == []
    button_events = [e for e in engine.broker.history if e.type == "button"]
    assert button_events[-1].button == "power"
    assert button_events[-1].detail == "unbound"


async def test_an_unknown_signature_falls_back_to_its_hex(buttons):
    engine = await make_engine(buttons)

    await engine.handle(press("DEADBEEF"))

    assert [e for e in engine.broker.history if e.type == "button"][-1].button == "DEADBEEF"


# --------------------------------------------------------------------------
# Pause -- log presses, do not act on them
# --------------------------------------------------------------------------


async def test_paused_a_bound_press_is_reported_but_not_run(buttons):
    engine = await make_engine(buttons)
    engine.paused = True

    await engine.handle(press())  # volume_up, bound to tv.volume_up

    assert commands(engine, "tv") == []
    button_events = [e for e in engine.broker.history if e.type == "button"]
    assert button_events[-1].button == "volume_up"
    assert button_events[-1].detail == "paused"


async def test_paused_a_release_after_a_press_also_does_not_run(buttons):
    """A press/release pair straddling a pause toggle must not leave a stray action."""
    engine = await make_engine(buttons)
    engine.paused = True

    await engine.handle(press())
    await engine.handle(press(kind="release"))

    assert commands(engine, "tv") == []


async def test_unpausing_lets_the_next_press_through(buttons):
    engine = await make_engine(buttons)
    engine.paused = True
    await engine.handle(press())
    assert commands(engine, "tv") == []

    engine.paused = False
    await engine.handle(press())

    assert commands(engine, "tv") == ["volume_up"]


async def test_paused_a_scene_switch_still_updates_active_scene_but_skips_its_macro(buttons):
    """Which scene a remote's activity button resolves to stays visible even
    paused; only the device commands that scene would have run are suppressed."""
    engine = await make_engine(buttons)
    engine.paused = True

    await engine.activate_scene("watch_tv")

    assert engine.active_scene == "watch_tv"
    assert commands(engine, "tv") == []
    action_events = [e for e in engine.broker.history if e.type == "action"]
    assert any("paused" in (e.detail or "") for e in action_events)


async def test_a_hold_timer_started_before_pausing_does_not_fire_while_paused(buttons):
    """Pausing mid-hold must cancel the pending timer, not just block new presses."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "power": {
                "on_press": [{"type": "device", "device": "tv", "command": "tap"}],
                "on_hold": [{"type": "device", "device": "tv", "command": "held"}],
                "hold_seconds": 0.02,
            }
        },
    )

    await engine.handle(press(POWER_SIGNATURE))
    engine.paused = True
    await asyncio.sleep(0.05)  # past hold_seconds -- the timer, if still armed, would have fired
    await engine.handle(press(POWER_SIGNATURE, kind="release"))

    assert commands(engine, "tv") == []


# --------------------------------------------------------------------------
# Scenes
# --------------------------------------------------------------------------


async def test_activating_a_scene_runs_its_start_macro(buttons):
    engine = await make_engine(buttons)

    await engine.activate_scene("watch_tv")

    assert commands(engine, "tv") == ["power_on"]
    assert engine.active_scene == "watch_tv"


async def test_switching_scenes_stops_the_previous_one_first(buttons):
    engine = await make_engine(buttons)
    await engine.activate_scene("watch_tv")

    await engine.activate_scene("music")

    assert commands(engine, "tv") == ["power_on", "power_off"]
    assert commands(engine, "amp") == ["power_on"]


async def test_reactivating_the_running_scene_is_a_no_op(buttons):
    """Pressing an activity button twice should not power-cycle everything."""
    engine = await make_engine(buttons)
    await engine.activate_scene("watch_tv")

    await engine.activate_scene("watch_tv")

    assert commands(engine, "tv") == ["power_on"]


async def test_stopping_a_scene_runs_its_stop_macro(buttons):
    engine = await make_engine(buttons)
    await engine.activate_scene("watch_tv")

    await engine.stop_scene()

    assert commands(engine, "tv") == ["power_on", "power_off"]
    assert engine.active_scene is None


async def test_a_button_can_switch_scenes(buttons):
    """The remote's own activity buttons work through an ordinary binding."""
    engine = await make_engine(
        buttons,
        global_bindings={"power": {"on_press": [{"type": "scene", "scene": "watch_tv"}]}},
    )

    await engine.handle(press(POWER_SIGNATURE))

    assert engine.active_scene == "watch_tv"
    assert commands(engine, "tv") == ["power_on"]


async def test_a_scene_action_with_no_scene_is_the_off_button(buttons):
    engine = await make_engine(
        buttons, global_bindings={"power": {"on_press": [{"type": "scene", "scene": None}]}}
    )
    await engine.activate_scene("watch_tv")

    await engine.handle(press(POWER_SIGNATURE))

    assert engine.active_scene is None
    assert commands(engine, "tv") == ["power_on", "power_off"]


async def test_the_default_scene_starts_with_the_engine(buttons):
    engine = SceneEngine(HubConfig.model_validate(config_dict(default_scene="watch_tv")), buttons)

    await engine.start()

    assert engine.active_scene == "watch_tv"


async def test_a_scene_loop_is_stopped_rather_than_spinning(buttons):
    """Two scenes that start each other must not hang the engine."""
    engine = await make_engine(
        buttons,
        scenes=[
            {"id": "a", "name": "A", "on_start": [{"type": "scene", "scene": "b"}]},
            {"id": "b", "name": "B", "on_start": [{"type": "scene", "scene": "a"}]},
        ],
        global_bindings={},
    )

    await asyncio.wait_for(engine.activate_scene("a"), timeout=5)

    assert any("nested too deeply" in (e.detail or "") for e in engine.broker.history)


# --------------------------------------------------------------------------
# Power policy across a scene switch
#
# Two scenes that both use the TV and AV receiver -- "watch shieldTV" and
# "watch TV" -- must not power-cycle either one just because the remote
# switched from one activity to the other. See `SceneEngine._stop_actions`.
# --------------------------------------------------------------------------


async def test_a_shared_device_is_not_power_cycled_by_a_scene_switch(buttons):
    """The bug this feature exists for."""
    engine = await make_engine(
        buttons,
        scenes=[
            {
                "id": "shieldtv", "name": "shieldTV", "devices": ["tv", "amp"],
                "on_start": [
                    {"type": "device", "device": "tv", "command": "power_on"},
                    {"type": "device", "device": "amp", "command": "power_on"},
                ],
                "on_stop": [
                    {"type": "device", "device": "tv", "command": "power_off"},
                    {"type": "device", "device": "amp", "command": "power_off"},
                ],
            },
            {
                "id": "watch_tv", "name": "Watch TV", "devices": ["tv", "amp"],
                "on_start": [{"type": "device", "device": "tv", "command": "play"}],
                "on_stop": [
                    {"type": "device", "device": "tv", "command": "power_off"},
                    {"type": "device", "device": "amp", "command": "power_off"},
                ],
            },
        ],
        global_bindings={},
    )
    await engine.activate_scene("shieldtv")

    await engine.activate_scene("watch_tv")

    assert commands(engine, "tv") == ["power_on", "play"]
    assert commands(engine, "amp") == ["power_on"]
    assert engine.active_scene == "watch_tv"


async def test_a_device_the_next_scene_does_not_need_still_powers_off(buttons):
    """The diff must not disable power-offs outright -- only devices the
    incoming scene actually needs are spared."""
    engine = await make_engine(
        buttons,
        scenes=[
            {
                "id": "shieldtv", "name": "shieldTV", "devices": ["tv"],
                "on_start": [{"type": "device", "device": "tv", "command": "power_on"}],
                "on_stop": [{"type": "device", "device": "tv", "command": "power_off"}],
            },
            {
                "id": "music", "name": "Music", "devices": ["amp"],
                "on_start": [{"type": "device", "device": "amp", "command": "power_on"}],
            },
        ],
        global_bindings={},
    )
    await engine.activate_scene("shieldtv")

    await engine.activate_scene("music")

    assert commands(engine, "tv") == ["power_on", "power_off"]


async def test_stopping_a_scene_still_powers_everything_off(buttons):
    """Going to idle -- an explicit stop, or the off button -- has no
    incoming scene to spare a device for, so the whole stop macro runs."""
    engine = await make_engine(
        buttons,
        scenes=[
            {
                "id": "shieldtv", "name": "shieldTV", "devices": ["tv", "amp"],
                "on_start": [
                    {"type": "device", "device": "tv", "command": "power_on"},
                    {"type": "device", "device": "amp", "command": "power_on"},
                ],
                "on_stop": [
                    {"type": "device", "device": "tv", "command": "power_off"},
                    {"type": "device", "device": "amp", "command": "power_off"},
                ],
            },
        ],
        global_bindings={},
    )
    await engine.activate_scene("shieldtv")

    await engine.stop_scene()

    assert commands(engine, "tv") == ["power_on", "power_off"]
    assert commands(engine, "amp") == ["power_on", "power_off"]


async def test_a_leave_on_device_survives_a_switch_but_not_an_explicit_stop(buttons):
    """`leave_on` stays powered through any scene switch -- even into a scene
    that does not use it at all -- and goes off only when its owning scene's
    stop macro actually runs, which an explicit stop does not spare it from.
    """
    devices = [
        {"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": TEST_COMMANDS}},
        {
            "id": "amp", "name": "Amp", "backend": "virtual",
            "config": {"commands": TEST_COMMANDS}, "power_policy": "leave_on",
        },
    ]
    scenes = [
        {
            "id": "shieldtv", "name": "shieldTV", "devices": ["tv", "amp"],
            "on_start": [{"type": "device", "device": "amp", "command": "power_on"}],
            "on_stop": [{"type": "device", "device": "amp", "command": "power_off"}],
        },
        {"id": "music", "name": "Music", "devices": ["tv"], "on_start": []},
    ]
    engine = await make_engine(buttons, devices=devices, scenes=scenes, global_bindings={})
    await engine.activate_scene("shieldtv")

    await engine.activate_scene("music")
    assert commands(engine, "amp") == ["power_on"]  # spared, though "music" does not need it

    await engine.activate_scene("shieldtv")
    await engine.stop_scene()
    assert commands(engine, "amp") == ["power_on", "power_on", "power_off"]


async def test_a_manual_device_gets_no_power_commands_from_scene_macros(buttons):
    """A `manual` device's policy promises the engine will never power it on
    or off on its own initiative -- a scene macro is exactly that kind of
    initiative. A button bound straight to a power command is not, and still
    reaches it.
    """
    devices = [
        {"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": TEST_COMMANDS}},
        {
            "id": "amp", "name": "Amp", "backend": "virtual",
            "config": {"commands": TEST_COMMANDS}, "power_policy": "manual",
        },
    ]
    scenes = [
        {
            "id": "shieldtv", "name": "shieldTV", "devices": ["tv", "amp"],
            "on_start": [{"type": "device", "device": "amp", "command": "power_on"}],
            "on_stop": [{"type": "device", "device": "amp", "command": "power_off"}],
        },
        {
            "id": "music", "name": "Music", "devices": ["amp"],
            "on_start": [{"type": "device", "device": "amp", "command": "power_on"}],
        },
    ]
    engine = await make_engine(
        buttons, devices=devices, scenes=scenes,
        global_bindings={"power": {"on_press": [{"type": "device", "device": "amp", "command": "power_off"}]}},
    )
    await engine.activate_scene("shieldtv")
    await engine.activate_scene("music")
    assert commands(engine, "amp") == []  # scene macros never touched it

    await engine.handle(press(POWER_SIGNATURE))
    assert commands(engine, "amp") == ["power_off"]  # a direct binding still does


async def test_a_scene_with_no_declared_devices_still_protects_them(buttons):
    """`Scene.required_devices()` falls back to what a scene's actions touch
    when `devices` is left empty -- a scene built by hand or through the API,
    not the app's device chips."""
    engine = await make_engine(
        buttons,
        scenes=[
            {
                "id": "shieldtv", "name": "shieldTV", "devices": ["tv"],
                "on_start": [{"type": "device", "device": "tv", "command": "power_on"}],
                "on_stop": [{"type": "device", "device": "tv", "command": "power_off"}],
            },
            {
                "id": "watch_tv", "name": "Watch TV",
                "on_start": [{"type": "device", "device": "tv", "command": "play"}],
            },
        ],
        global_bindings={},
    )
    await engine.activate_scene("shieldtv")

    await engine.activate_scene("watch_tv")

    assert commands(engine, "tv") == ["power_on", "play"]


async def test_a_stop_macro_of_nothing_but_delays_is_skipped(buttons):
    """Once every action after a delay has been filtered out, the delay
    itself accomplishes nothing and must not run either."""
    engine = await make_engine(
        buttons,
        scenes=[
            {
                "id": "shieldtv", "name": "shieldTV", "devices": ["tv"],
                "on_start": [{"type": "device", "device": "tv", "command": "power_on"}],
                "on_stop": [
                    {"type": "delay", "seconds": 5},
                    {"type": "device", "device": "tv", "command": "power_off"},
                ],
            },
            {"id": "watch_tv", "name": "Watch TV", "devices": ["tv"], "on_start": []},
        ],
        global_bindings={},
    )
    await engine.activate_scene("shieldtv")

    await asyncio.wait_for(engine.activate_scene("watch_tv"), timeout=1)

    assert commands(engine, "tv") == ["power_on"]


async def test_a_transition_does_not_flicker_through_idle(buttons):
    """Switching directly between two scenes publishes one `scene` event, not
    "stopped" followed by "started" -- there is no idle moment in between."""
    engine = await make_engine(buttons)
    await engine.activate_scene("watch_tv")
    before = len(engine.broker.history)

    await engine.activate_scene("music")

    scene_events = [e for e in engine.broker.history[before:] if e.type == "scene"]
    assert len(scene_events) == 1
    assert scene_events[0].scene == "music"


# --------------------------------------------------------------------------
# Press phases
# --------------------------------------------------------------------------


async def test_repeat_runs_the_repeat_actions(buttons):
    """Volume ramps while held; that is what on_repeat is for."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                # No delay: ramp from the first packet, the way a button
                # meant purely for ramping should.
                "repeat_delay": 0,
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    for _ in range(3):
        await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"] * 3


async def test_a_short_press_does_not_auto_repeat(buttons):
    """One quick press must do its thing once, not three or four times.

    The remote reports a held button every ~100ms and never says how long it
    has been down, so an ordinary 300ms press arrives as a press and three
    repeats. Without a delay each one fires, which is indistinguishable from
    the button having been pressed four times.
    """
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "on_press": [{"type": "device", "device": "tv", "command": "volume_up"}],
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    for _ in range(3):  # ~300ms of being held, arriving back to back
        await engine.handle(press(kind="repeat"))
    await engine.handle(press(kind="release"))

    assert commands(engine, "tv") == ["volume_up"]


async def test_repeats_start_once_the_button_really_has_been_held(buttons):
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "repeat_delay": 0.05,
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))
    assert commands(engine, "tv") == []  # too soon to be a hold

    await asyncio.sleep(0.06)
    await engine.handle(press(kind="repeat"))
    await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"] * 2


async def test_the_repeat_interval_caps_how_fast_a_ramp_fires(buttons):
    """The remote's ~100ms cadence is faster than some equipment wants."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "repeat_delay": 0,
                "repeat_interval": 0.05,
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    for _ in range(4):  # four packets in far less than one interval
        await engine.handle(press(kind="repeat"))
    assert commands(engine, "tv") == ["volume_up"]

    await asyncio.sleep(0.06)
    await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"] * 2


async def test_releasing_and_pressing_again_starts_the_delay_over(buttons):
    """Two quick taps are two taps, not a tap and the start of a ramp."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "repeat_delay": 0.05,
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    await asyncio.sleep(0.06)
    await engine.handle(press(kind="repeat"))
    assert commands(engine, "tv") == ["volume_up"]

    await engine.handle(press(kind="release"))
    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"]  # unchanged: the clock reset


async def test_a_binding_with_no_repeat_timing_of_its_own_follows_the_config_default(buttons):
    """The common case: one setting for the whole remote, not one per button."""
    engine = await make_engine(
        buttons,
        default_repeat_delay=0,  # config-wide: repeat immediately
        global_bindings={
            "volume_up": {"on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}]}
        },
    )

    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"]


async def test_a_bindings_own_repeat_delay_overrides_the_config_default(buttons):
    """The escape hatch: one button that needs different timing from the rest."""
    engine = await make_engine(
        buttons,
        default_repeat_delay=5,  # config-wide: nothing should repeat in this test
        global_bindings={
            "volume_up": {
                "repeat_delay": 0,  # this button overrides it back to instant
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"]


async def test_a_bindings_own_repeat_interval_overrides_the_config_default(buttons):
    engine = await make_engine(
        buttons,
        default_repeat_delay=0,
        default_repeat_interval=5,  # config-wide: heavily throttled
        global_bindings={
            "volume_up": {
                "repeat_interval": 0,  # this button overrides it back to uncapped
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    for _ in range(3):
        await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"] * 3


async def test_changing_the_config_default_affects_every_binding_that_did_not_override_it(buttons):
    """The point of a global default: change it once, every plain button follows."""
    engine = await make_engine(
        buttons,
        default_repeat_delay=5,
        global_bindings={
            "volume_up": {"on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}]}
        },
    )

    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))
    assert commands(engine, "tv") == []  # config default of 5s: far too soon

    await engine.reload(HubConfig.model_validate(config_dict(
        default_repeat_delay=0,
        global_bindings={
            "volume_up": {"on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}]}
        },
    )))
    await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"]


# --------------------------------------------------------------------------
# Repeat acceleration
# --------------------------------------------------------------------------


async def test_repeat_accel_off_by_default_behaves_like_a_flat_repeat(buttons):
    """`repeat_accel` at its default of 1.0 must not change anything -- a
    binding that never mentions it should repeat exactly as it always has."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "repeat_delay": 0,
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    for _ in range(4):
        await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"] * 4


async def test_repeat_accel_fires_the_repeat_actions_more_than_once_per_packet(buttons):
    """Once the ramp has had time to build up, a single held-button packet
    should fire the repeat actions several times, not just once -- the
    remote never reports a hold any faster than its own ~100ms cadence, so
    going faster than that has to mean more repeats per packet instead of
    more packets."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "repeat_delay": 0,
                "repeat_interval": 0,
                "repeat_accel": 4,
                "repeat_accel_seconds": 0.05,
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))  # first packet past the delay: exactly one
    assert commands(engine, "tv") == ["volume_up"]

    await asyncio.sleep(0.08)  # past repeat_accel_seconds: the ramp is now maxed out
    await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"] * (1 + 4)


async def test_repeat_accel_never_exceeds_the_hard_burst_ceiling(buttons):
    """However high `repeat_accel` is configured, one packet must not be
    able to flood a backend with an unbounded number of commands."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "repeat_delay": 0,
                "repeat_interval": 0,
                "repeat_accel": 16,  # the maximum allowed value
                "repeat_accel_seconds": 0.05,
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))
    await asyncio.sleep(0.08)
    await engine.handle(press(kind="repeat"))

    # 16x would be 16 repeats for this one packet; the engine caps it well
    # below that regardless of how the ramp is configured.
    assert len(commands(engine, "tv")) <= 1 + 8


async def test_releasing_resets_the_acceleration_ramp(buttons):
    """A fresh press must start slow again, not pick up where a previous
    hold's ramp left off."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "repeat_delay": 0,
                "repeat_interval": 0,
                "repeat_accel": 8,
                "repeat_accel_seconds": 0.05,
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))
    await asyncio.sleep(0.08)
    await engine.handle(press(kind="repeat"))  # ramp maxed out: a burst of up to 8
    burst = len(commands(engine, "tv"))
    assert burst > 2

    await engine.handle(press(kind="release"))
    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))  # first packet of the new press: exactly one
    await engine.handle(press(kind="repeat"))  # immediately after: ramp restarted, so still slow

    assert commands(engine, "tv")[burst:] == ["volume_up", "volume_up"]


async def test_a_bindings_own_repeat_accel_overrides_the_config_default(buttons):
    engine = await make_engine(
        buttons,
        default_repeat_accel=1,  # config-wide: acceleration off
        default_repeat_accel_seconds=0.05,
        global_bindings={
            "volume_up": {
                "repeat_delay": 0,
                "repeat_interval": 0,
                "repeat_accel": 4,  # this button overrides it back on
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))
    await asyncio.sleep(0.08)
    await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == ["volume_up"] * (1 + 4)


async def test_a_burst_of_repeats_is_announced_once_but_failures_are_not_silenced(buttons):
    """Logging every repeat in an eight-wide burst would flood the live view
    for no benefit, so only the first is announced -- but a failure part way
    through a burst must still be visible every time it happens."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "repeat_delay": 0,
                "repeat_interval": 0,
                "repeat_accel": 4,
                "repeat_accel_seconds": 0.05,
                "on_repeat": [{"type": "device", "device": "tv", "command": "not_a_real_command"}],
            }
        },
    )

    before = len(engine.broker.history)
    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))
    await asyncio.sleep(0.08)
    await engine.handle(press(kind="repeat"))  # a burst of failures

    failures = [e for e in engine.broker.history[before:] if e.type == "action" and e.ok is False]
    assert len(failures) == 1 + 4  # one per iteration of the burst, not just the first


async def test_a_burst_of_successful_repeats_publishes_one_labelled_action_event(buttons):
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "repeat_delay": 0,
                "repeat_interval": 0,
                "repeat_accel": 4,
                "repeat_accel_seconds": 0.05,
                "on_repeat": [{"type": "device", "device": "tv", "command": "volume_up"}],
            }
        },
    )

    before = len(engine.broker.history)
    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))
    await asyncio.sleep(0.08)
    await engine.handle(press(kind="repeat"))  # a burst of 4 successful repeats

    action_events = [e for e in engine.broker.history[before:] if e.type == "action" and e.ok]
    # Two packets fired in total (the first single repeat, then the burst),
    # so two action events -- not one per underlying backend call -- and the
    # second names how many it actually stood for.
    assert len(action_events) == 2
    assert "×4" in action_events[-1].action


async def test_release_runs_the_release_actions(buttons):
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {"on_release": [{"type": "device", "device": "tv", "command": "stop"}]}
        },
    )

    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="release"))

    assert commands(engine, "tv") == ["stop"]


async def test_a_button_without_a_hold_action_fires_immediately(buttons):
    """Only buttons that use holds should pay the latency of waiting."""
    engine = await make_engine(buttons)

    await engine.handle(press())

    assert commands(engine, "tv") == ["volume_up"]


async def test_a_short_press_on_a_hold_button_fires_the_tap_action(buttons):
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "hold_seconds": 5,
                "on_press": [{"type": "device", "device": "tv", "command": "tap"}],
                "on_hold": [{"type": "device", "device": "tv", "command": "held"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    assert commands(engine, "tv") == []  # deferred: could still become a hold

    await engine.handle(press(kind="release"))
    assert commands(engine, "tv") == ["tap"]


async def test_a_long_press_fires_the_hold_action_and_not_the_tap(buttons):
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "hold_seconds": 0.05,
                "on_press": [{"type": "device", "device": "tv", "command": "tap"}],
                "on_hold": [{"type": "device", "device": "tv", "command": "held"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    await asyncio.sleep(0.15)
    await engine.handle(press(kind="release"))

    assert commands(engine, "tv") == ["held"]


async def test_repeats_are_suppressed_while_a_hold_is_undecided(buttons):
    """Otherwise the repeat would run before the press it belongs to."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "hold_seconds": 5,
                "on_press": [{"type": "device", "device": "tv", "command": "tap"}],
                "on_repeat": [{"type": "device", "device": "tv", "command": "ramp"}],
                "on_hold": [{"type": "device", "device": "tv", "command": "held"}],
            }
        },
    )

    await engine.handle(press(kind="press"))
    await engine.handle(press(kind="repeat"))

    assert commands(engine, "tv") == []


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


async def test_a_failing_action_does_not_stop_the_rest_of_the_macro(buttons):
    """A power-on that fails must not block the input-select behind it."""
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "on_press": [
                    {"type": "device", "device": "tv", "command": "first"},
                    {"type": "device", "device": "amp", "command": "boom"},
                    {"type": "device", "device": "tv", "command": "third"},
                ]
            }
        },
    )
    # The runtime case config validation cannot catch: the device exists in
    # configuration but its backend failed to start.
    engine._backends.pop("amp")

    await engine.handle(press())

    assert commands(engine, "tv") == ["first", "third"]
    assert any(e.type == "action" and e.ok is False for e in engine.broker.history)


async def test_a_delay_action_sequences_a_macro(buttons):
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "on_press": [
                    {"type": "device", "device": "tv", "command": "power_on"},
                    {"type": "delay", "seconds": 0.05},
                    {"type": "device", "device": "tv", "command": "set_input"},
                ]
            }
        },
    )

    await engine.handle(press())

    calls = engine.backend_for("tv").calls
    assert [c["command"] for c in calls] == ["power_on", "set_input"]
    assert calls[1]["at"] - calls[0]["at"] >= 0.05


async def test_reload_keeps_a_still_valid_active_scene(buttons):
    """Saving an edit in the UI should not silently stop what is running."""
    engine = await make_engine(buttons)
    await engine.activate_scene("watch_tv")

    await engine.reload(HubConfig.model_validate(config_dict()))

    assert engine.active_scene == "watch_tv"


async def test_reload_drops_an_active_scene_that_was_deleted(buttons):
    engine = await make_engine(buttons)
    await engine.activate_scene("music")

    await engine.reload(HubConfig.model_validate(config_dict(scenes=[], global_bindings={})))

    assert engine.active_scene is None


async def test_a_device_that_fails_to_start_does_not_stop_the_others(buttons):
    engine = SceneEngine(
        HubConfig.model_validate(
            config_dict(devices=[{"id": "tv", "name": "TV", "backend": "does_not_exist"}], scenes=[])
        ),
        buttons,
    )

    await engine.start()

    assert engine.backend_for("tv") is None
    assert any(e.ok is False for e in engine.broker.history)


async def test_stop_cancels_a_pending_hold(buttons):
    engine = await make_engine(
        buttons,
        global_bindings={
            "volume_up": {
                "hold_seconds": 0.05,
                "on_press": [{"type": "device", "device": "tv", "command": "tap"}],
                "on_hold": [{"type": "device", "device": "tv", "command": "held"}],
            }
        },
    )
    tv = engine.backend_for("tv")  # stop() clears the registry, so grab it first
    await engine.handle(press(kind="press"))

    await engine.stop()
    await asyncio.sleep(0.15)

    assert tv.calls == []


async def test_the_learned_button_name_is_used_not_the_hid_fallback(buttons):
    """A button the operator named should read that way in the UI."""
    buttons.learn("volume_up", "TV Louder", VOLUME_UP_SIGNATURE)
    engine = await make_engine(buttons)

    await engine.handle(press())

    assert [e for e in engine.broker.history if e.type == "button"][-1].label == "TV Louder"


async def test_an_unknown_button_still_reports_a_usable_name(buttons):
    engine = await make_engine(buttons)

    await engine.handle(press("DEADBEEF"))

    event = [e for e in engine.broker.history if e.type == "button"][-1]
    assert event.button == "DEADBEEF"
    assert event.label


# --------------------------------------------------------------------------
# Focus and the SmartHome +/- keys
# --------------------------------------------------------------------------


async def test_a_successful_device_action_that_claims_a_target_sets_the_focus(buttons):
    engine = await make_focus_engine(buttons)

    await engine.handle(press())  # volume_up -> amp.toggle, which claims "lamp"

    assert engine.focus == Focus(device="amp", target="lamp", label="Lamp")


async def test_a_device_action_that_claims_nothing_does_not_set_the_focus(buttons):
    engine = await make_engine(buttons)  # plain devices: nothing ever claims a focus

    await engine.handle(press())

    assert engine.focus is None


async def test_a_failed_device_action_does_not_set_the_focus(buttons):
    engine = await make_engine(
        buttons,
        devices=FOCUS_DEVICES,
        global_bindings={"volume_up": {"on_press": [{"type": "device", "device": "amp", "command": "nope"}]}},
    )

    await engine.handle(press())

    assert engine.focus is None
    assert any(e.type == "action" and e.ok is False for e in engine.broker.history)


async def test_adjust_steps_whatever_is_currently_focused(buttons):
    engine = await make_focus_engine(buttons)
    await engine.handle(press())  # sets the focus to "lamp"

    await engine.handle(press(POWER_SIGNATURE))  # adjust up

    assert commands(engine, "amp")[-1] == "brighter"


async def test_adjust_does_not_move_the_focus(buttons):
    """Ramping must not re-stamp what it is ramping."""
    engine = await make_focus_engine(buttons)
    await engine.handle(press())

    await engine.handle(press(POWER_SIGNATURE))

    assert engine.focus == Focus(device="amp", target="lamp", label="Lamp")


async def test_adjust_with_nothing_focused_reports_and_does_not_raise(buttons):
    engine = await make_focus_engine(buttons)

    await engine.handle(press(POWER_SIGNATURE))  # nothing has been touched yet

    assert commands(engine, "amp") == []
    assert any(e.type == "action" and e.ok is False for e in engine.broker.history)


async def test_adjust_on_a_non_adjustable_focus_reports_and_sends_nothing(buttons):
    engine = await make_engine(
        buttons,
        devices=[
            FOCUS_DEVICES[0],
            {
                "id": "amp",
                "name": "Amp",
                "backend": "virtual",
                # Claims a focus but offers no way to step it -- a socket,
                # not a light.
                "config": {"commands": TEST_COMMANDS, "focus": {"toggle": ["socket", "Socket"]}},
            },
        ],
        global_bindings={
            "volume_up": {"on_press": [{"type": "device", "device": "amp", "command": "toggle"}]},
            "power": {"on_press": [{"type": "adjust", "direction": "up"}]},
        },
    )
    await engine.handle(press())  # focus -> "socket"

    await engine.handle(press(POWER_SIGNATURE))

    assert commands(engine, "amp") == ["toggle"]
    assert any(e.type == "action" and e.ok is False for e in engine.broker.history)


async def test_focus_survives_a_scene_switch(buttons):
    """Which light was last touched has nothing to do with which scene is running."""
    engine = await make_focus_engine(buttons)
    await engine.handle(press())

    await engine.activate_scene("music")

    assert engine.focus == Focus(device="amp", target="lamp", label="Lamp")


async def test_stop_clears_the_focus(buttons):
    engine = await make_focus_engine(buttons)
    await engine.handle(press())

    await engine.stop()

    assert engine.focus is None


async def test_reload_drops_the_focus_if_its_device_is_removed(buttons):
    engine = await make_focus_engine(buttons)
    await engine.handle(press())

    await engine.reload(
        HubConfig.model_validate(config_dict(devices=[FOCUS_DEVICES[0]], scenes=[], global_bindings={}))
    )

    assert engine.focus is None


async def test_reload_keeps_the_focus_if_its_device_still_exists(buttons):
    engine = await make_focus_engine(buttons)
    await engine.handle(press())

    await engine.reload(HubConfig.model_validate(config_dict(devices=FOCUS_DEVICES)))

    assert engine.focus == Focus(device="amp", target="lamp", label="Lamp")


async def test_the_fallback_target_is_used_when_nothing_has_been_touched(buttons):
    engine = await make_engine(
        buttons,
        devices=FOCUS_DEVICES,
        global_bindings={
            "power": {"on_press": [{"type": "adjust", "direction": "up", "device": "amp", "target": "lamp"}]}
        },
    )

    await engine.handle(press(POWER_SIGNATURE))

    assert commands(engine, "amp") == ["brighter"]


async def test_the_fallback_is_ignored_once_something_has_been_touched(buttons):
    engine = await make_engine(
        buttons,
        devices=FOCUS_DEVICES,
        global_bindings={
            "volume_up": {"on_press": [{"type": "device", "device": "amp", "command": "toggle"}]},
            # Names a target with no adjust mapping of its own -- proving it
            # is never consulted once something real has been touched.
            "power": {"on_press": [{"type": "adjust", "direction": "down", "device": "amp", "target": "ghost"}]},
        },
    )
    await engine.handle(press())  # focus -> "lamp", not the fallback's "ghost"

    await engine.handle(press(POWER_SIGNATURE))

    assert commands(engine, "amp")[-1] == "dimmer"


async def test_touching_the_same_target_again_does_not_reannounce_the_focus(buttons):
    """Holding a repeatable command on an already-focused target must not
    flood the log with the same announcement on every repeat."""
    engine = await make_focus_engine(buttons)

    await engine.handle(press())  # first toggle: claims "lamp", announces it
    await engine.handle(press())  # toggled again: same target, nothing new to say

    announcements = [e for e in engine.broker.history if e.type == "status" and "follow" in (e.detail or "")]
    assert len(announcements) == 1
