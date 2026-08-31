"""`TransitionValue` -- which scene a switch is moving from and to.

Driven entirely by the virtual backend and `set` actions that capture what a
transition resolved to, so the answer at each point in a switch can be
asserted on directly. No radio, no network.
"""

from __future__ import annotations

import pytest

from harmony_hub.engine import SceneEngine
from harmony_hub.models import HubConfig
from harmony_receiver.profiles import ButtonMap

TEST_COMMANDS = ["power_on", "power_off"]

FROM = {"type": "transition", "edge": "from"}
TO = {"type": "transition", "edge": "to"}


@pytest.fixture
def buttons() -> ButtonMap:
    return ButtonMap()


def capture(name: str, edge: dict) -> dict:
    """A `set` action that records what a transition side resolved to,
    right where it is evaluated -- the only way to observe a value that is
    only in scope while a macro is actually running.
    """
    return {"type": "set", "name": name, "value": edge}


def tv_device() -> dict:
    return {"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": TEST_COMMANDS}}


async def make_engine(scenes: list[dict], buttons: ButtonMap, **overrides) -> SceneEngine:
    engine = SceneEngine(
        HubConfig.model_validate(
            {"devices": [tv_device()], "scenes": scenes, "global_scene": None, **overrides}
        ),
        buttons,
    )
    await engine.start()
    return engine


# --------------------------------------------------------------------------
# Basic from/to resolution
# --------------------------------------------------------------------------


async def test_starting_from_idle_the_from_side_is_empty(buttons):
    engine = await make_engine(
        [{"id": "a", "name": "A", "on_start": [capture("from", FROM), capture("to", TO)]}], buttons
    )
    await engine.activate_scene("a")

    assert engine.variables == {"from": "", "to": "a"}


async def test_on_start_sees_which_scene_it_came_from(buttons):
    engine = await make_engine(
        [
            {"id": "a", "name": "A"},
            {"id": "b", "name": "B", "on_start": [capture("from", FROM), capture("to", TO)]},
        ],
        buttons,
    )
    await engine.activate_scene("a")
    await engine.activate_scene("b")

    assert engine.variables == {"from": "a", "to": "b"}


async def test_on_stop_sees_the_same_transition_on_start_will_see(buttons):
    """The whole point: both ends of one switch see one answer, not two."""
    engine = await make_engine(
        [
            {"id": "a", "name": "A", "on_stop": [capture("stop_from", FROM), capture("stop_to", TO)]},
            {"id": "b", "name": "B", "on_start": [capture("start_from", FROM), capture("start_to", TO)]},
        ],
        buttons,
    )
    await engine.activate_scene("a")
    await engine.activate_scene("b")

    assert engine.variables == {
        "stop_from": "a", "stop_to": "b",
        "start_from": "a", "start_to": "b",
    }


async def test_stopping_to_idle_the_to_side_is_empty(buttons):
    engine = await make_engine(
        [{"id": "a", "name": "A", "on_stop": [capture("from", FROM), capture("to", TO)]}], buttons
    )
    await engine.activate_scene("a")
    await engine.stop_scene()

    assert engine.variables == {"from": "a", "to": ""}


async def test_the_default_scene_at_startup_comes_from_idle(buttons):
    engine = SceneEngine(
        HubConfig.model_validate(
            {
                "devices": [tv_device()],
                "scenes": [{"id": "a", "name": "A", "on_start": [capture("from", FROM)]}],
                "default_scene": "a",
            }
        ),
        buttons,
    )
    await engine.start()

    assert engine.variables == {"from": ""}


async def test_a_button_binding_outside_any_switch_reads_both_sides_empty(buttons):
    """No transition is running for an ordinary press -- both sides answer
    `""`, the same as idle, rather than raising into `on_unreadable`.
    """
    engine = await make_engine(
        [
            {
                "id": "a", "name": "A",
                "bindings": {"power": {"on_press": [capture("from", FROM), capture("to", TO)]}},
            }
        ],
        buttons,
    )
    await engine.activate_scene("a")
    engine.variables.clear()

    await engine.run_actions(
        engine.config.scene("a").bindings["power"].on_press, source="power.press"
    )

    assert engine.variables == {"from": "", "to": ""}


# --------------------------------------------------------------------------
# Nesting: a stop macro that itself switches scenes
# --------------------------------------------------------------------------


async def test_a_nested_switch_restores_the_outer_transition_afterwards(buttons):
    """A's on_stop switches to C partway through -- a real pattern (an
    "everything off" scene stopping another) -- and C's own start/stop
    macros run inside that nested switch with their own transition. Once it
    returns, the rest of A's on_stop (still logically part of the A -> B
    switch the outer `activate_scene` is doing) must see A -> B again, not
    whatever C's switch left behind.
    """
    engine = await make_engine(
        [
            {
                "id": "a", "name": "A",
                "on_stop": [
                    capture("before_from", FROM),
                    capture("before_to", TO),
                    {"type": "scene", "scene": "c"},
                    capture("after_from", FROM),
                    capture("after_to", TO),
                ],
            },
            {"id": "b", "name": "B"},
            {"id": "c", "name": "C"},
        ],
        buttons,
    )
    await engine.activate_scene("a")
    await engine.activate_scene("b")

    # Whatever "c" believed from/to were during its own switch is irrelevant
    # here -- what matters is that A's stop macro sees A -> B on both sides
    # of the nested detour into C.
    assert engine.variables["before_from"] == "a"
    assert engine.variables["before_to"] == "b"
    assert engine.variables["after_from"] == "a"
    assert engine.variables["after_to"] == "b"


# --------------------------------------------------------------------------
# Composed with `if` -- the actual feature request
# --------------------------------------------------------------------------


async def test_an_if_can_branch_on_which_scene_is_being_left(buttons):
    """"if coming from watch_tv, skip powering the TV on" -- the scenario
    that motivated this feature.
    """
    engine = await make_engine(
        [
            {"id": "watch_tv", "name": "Watch TV"},
            {"id": "gaming", "name": "Gaming"},
            {
                "id": "listen_music", "name": "Listen to Music", "devices": ["tv"],
                "on_start": [
                    {
                        "type": "if",
                        "condition": {
                            "left": FROM, "op": "is",
                            "right": {"type": "literal", "value": "watch_tv"},
                        },
                        "then": [],
                        "otherwise": [{"type": "device", "device": "tv", "command": "power_on"}],
                    }
                ],
            },
        ],
        buttons,
    )

    await engine.activate_scene("watch_tv")
    await engine.activate_scene("listen_music")
    assert [c["command"] for c in engine.backend_for("tv").calls] == []

    await engine.stop_scene()
    await engine.activate_scene("gaming")
    await engine.activate_scene("listen_music")
    assert [c["command"] for c in engine.backend_for("tv").calls] == ["power_on"]


# --------------------------------------------------------------------------
# The published event
# --------------------------------------------------------------------------


async def test_the_scene_event_reports_from_scene(buttons):
    engine = await make_engine([{"id": "a", "name": "A"}, {"id": "b", "name": "B"}], buttons)

    await engine.activate_scene("a")
    await engine.activate_scene("b")
    await engine.stop_scene()

    scene_events = [e for e in engine.broker.history if e.type == "scene"]
    assert scene_events[0].scene == "a" and scene_events[0].from_scene is None
    assert scene_events[1].scene == "b" and scene_events[1].from_scene == "a"
    assert scene_events[2].scene is None and scene_events[2].from_scene == "b"
