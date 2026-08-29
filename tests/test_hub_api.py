"""API surface, driven end to end against a real hub with no radio.

Uses TestClient rather than calling the service directly, so these cover the
actual contract the UI is written against -- routing, serialisation and the
lifespan wiring included.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from harmony_hub.api import create_app
from harmony_hub.service import HubSettings

CONFIG = {
    "version": 1,
    "devices": [{"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": ["on", "off"]}}],
    "scenes": [
        {
            "id": "watch_tv",
            "name": "Watch TV",
            "devices": ["tv"],
            "on_start": [{"type": "device", "device": "tv", "command": "on"}],
            "bindings": {"volume_up": {"on_press": [{"type": "device", "device": "tv", "command": "on"}]}},
        }
    ],
    "global_scene": None,
}

BUTTONS = {
    "volume_up": {"label": "Volume Up", "signatures": ["C3E90000"]},
    "power": {"label": "Power", "signatures": ["C3300000"]},
}


@pytest.fixture
def client(tmp_path):
    config_path = tmp_path / "hub_config.json"
    buttons_path = tmp_path / "buttons.json"
    config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
    buttons_path.write_text(json.dumps(BUTTONS), encoding="utf-8")

    # `settings_path` always goes to tmp_path: saving settings writes it, and
    # without this the default lands in the repository's working directory.
    app = create_app(
        HubSettings(config_path=config_path, buttons_path=buttons_path),
        settings_path=tmp_path / "hub_settings.json",
    )
    with TestClient(app) as client:
        client.config_path = config_path
        yield client


def test_state_reports_scenes_devices_and_buttons(client):
    state = client.get("/api/state").json()

    assert state["active_scene"] is None
    assert state["scenes"][0]["id"] == "watch_tv"
    assert state["scenes"][0]["bound_buttons"] == 1
    assert state["devices"][0] == {
        "id": "tv", "name": "TV", "backend": "virtual",
        "running": True, "ok": True, "detail": "0 command(s) recorded",
    }
    assert state["button_count"] == 2
    assert state["focus"] is None


def test_buttons_come_from_the_receiver_profile(client):
    buttons = client.get("/api/buttons").json()

    assert {b["key"] for b in buttons} == {"volume_up", "power"}
    assert next(b for b in buttons if b["key"] == "volume_up")["signatures"] == ["C3E90000"]


def test_backends_expose_a_config_schema_for_the_device_form(client):
    """The UI generates the device form from this instead of hard-coding each backend."""
    names = {b["name"]: b for b in client.get("/api/backends").json()}

    assert {"virtual", "http", "shell", "androidtv"} <= set(names)
    assert names["http"]["config_schema"]["properties"]["base_url"]["title"] == "Base URL"


def test_backends_say_whether_they_need_pairing(client):
    """The app shows its Pair button off this, rather than knowing backend names."""
    names = {b["name"]: b for b in client.get("/api/backends").json()}

    assert names["androidtv"]["pairable"] is True
    assert names["virtual"]["pairable"] is False


def test_backends_say_whether_they_can_find_themselves_on_the_network(client):
    """The app shows its 'Find on the network' button off this too."""
    names = {b["name"]: b for b in client.get("/api/backends").json()}

    assert names["androidtv"]["discoverable"] is True
    assert names["androidtv"]["discover_field"] == "host"
    assert names["homeassistant"]["discoverable"] is True
    assert names["homeassistant"]["discover_field"] == "url"
    assert names["denon"]["discoverable"] is True
    assert names["denon"]["discover_field"] == "host"
    assert names["lgtv"]["discoverable"] is True
    assert names["lgtv"]["discover_field"] == "host"
    assert names["virtual"]["discoverable"] is False
    assert names["virtual"]["discover_field"] == ""


def test_denon_discovery_reaches_the_backends_own_discover_function(client, monkeypatch):
    """Same route shape as the other network backends, just a different wire."""
    from harmony_hub.backends import denon

    async def fake_discover(timeout: float = 3.0):
        return [{"name": "Living Room AVR", "host": "10.0.0.7", "version": "AVR-X2700H"}]

    monkeypatch.setattr(denon, "discover", fake_discover)

    found = client.get("/api/backends/denon/discover").json()

    assert found == [{"name": "Living Room AVR", "host": "10.0.0.7", "version": "AVR-X2700H"}]


def test_lgtv_discovery_reaches_the_backends_own_discover_function(client, monkeypatch):
    """Same route shape again, over yet another wire (SSDP, no mDNS)."""
    from harmony_hub.backends import lgtv

    async def fake_discover(timeout: float = 3.0):
        return [{"name": "Lounge TV", "host": "10.0.0.9", "model": "OLED55C2"}]

    monkeypatch.setattr(lgtv, "discover", fake_discover)

    found = client.get("/api/backends/lgtv/discover").json()

    assert found == [{"name": "Lounge TV", "host": "10.0.0.9", "model": "OLED55C2"}]


def test_device_commands_drive_the_binding_editor(client):
    commands = client.get("/api/devices/tv/commands").json()

    assert [c["name"] for c in commands] == ["on", "off"]


def test_commands_for_an_unknown_device_are_a_404(client):
    assert client.get("/api/devices/ghost/commands").status_code == 404


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

PAIRING_CONFIG = {
    "version": 1,
    "devices": [
        {"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": ["on"]}},
        # No address, so it starts but cannot reach anything -- which is the
        # state a device is in between being added and being paired.
        {"id": "shield", "name": "Shield", "backend": "androidtv", "config": {}},
    ],
    "scenes": [],
    "global_scene": None,
}


@pytest.fixture
def pairing_client(tmp_path):
    config_path = tmp_path / "hub_config.json"
    buttons_path = tmp_path / "buttons.json"
    config_path.write_text(json.dumps(PAIRING_CONFIG), encoding="utf-8")
    buttons_path.write_text(json.dumps(BUTTONS), encoding="utf-8")

    app = create_app(
        HubSettings(config_path=config_path, buttons_path=buttons_path),
        settings_path=tmp_path / "hub_settings.json",
    )
    with TestClient(app) as client:
        yield client


def test_a_device_that_cannot_connect_still_starts(pairing_client):
    """Otherwise it would never be reachable by the pairing routes, and an
    unpaired device could never become a paired one."""
    devices = {d["id"]: d for d in pairing_client.get("/api/state").json()["devices"]}

    assert devices["shield"]["running"] is True
    assert devices["shield"]["ok"] is False
    assert devices["shield"]["detail"] == "no address set"


def test_pairing_reports_its_problem_rather_than_failing(pairing_client):
    result = pairing_client.post("/api/devices/shield/pair/start").json()

    assert result["ok"] is False
    assert "no address set" in result["detail"]


def test_pairing_a_backend_that_does_not_pair_is_a_400(pairing_client):
    response = pairing_client.post("/api/devices/tv/pair/start")

    assert response.status_code == 400
    assert "does not need pairing" in response.json()["detail"]


def test_pairing_an_unknown_device_is_a_404(pairing_client):
    assert pairing_client.post("/api/devices/ghost/pair/start").status_code == 404
    assert pairing_client.post("/api/devices/ghost/pair/finish", json={"code": "1"}).status_code == 404


def test_suggested_bindings_map_real_buttons_to_real_commands(pairing_client):
    bindings = pairing_client.get("/api/devices/shield/suggested_bindings").json()["bindings"]
    commands = {c["name"] for c in pairing_client.get("/api/devices/shield/commands").json()}

    assert bindings["volume_up"] == "volume_up"
    assert bindings["ac_back"] == "back"
    assert set(bindings.values()) <= commands


def test_a_backend_with_no_suggestion_returns_an_empty_map(pairing_client):
    assert pairing_client.get("/api/devices/tv/suggested_bindings").json() == {"bindings": {}, "adjust": {}}


def test_commands_say_which_ones_may_repeat_while_held(pairing_client):
    commands = {c["name"]: c for c in pairing_client.get("/api/devices/shield/commands").json()}

    assert commands["volume_up"]["repeatable"] is True
    assert commands["power_on"]["repeatable"] is False


# ---------------------------------------------------------------------------
# Choosing what a device offers
#
# Home Assistant is the one backend whose command list is not fixed: it is
# drawn from hundreds of entities that differ in every house, so the app has
# to be able to show them and let someone pick.
# ---------------------------------------------------------------------------

HA_CONFIG = {
    "version": 1,
    "devices": [
        {"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": ["on"]}},
        {
            "id": "house",
            "name": "House",
            "backend": "homeassistant",
            "config": {"url": "http://ha.local:8123", "entities": ["light.kitchen"]},
        },
    ],
    "scenes": [],
    "global_scene": None,
}

HA_STATES = [
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
    {"entity_id": "switch.amp", "state": "off", "attributes": {"friendly_name": "Amplifier"}},
    {"entity_id": "sensor.temperature", "state": "19.4", "attributes": {"friendly_name": "Hallway"}},
]


@pytest.fixture
def ha_client(tmp_path, monkeypatch):
    """A hub with a paired Home Assistant device behind a fake instance."""
    import httpx

    from harmony_hub.backends import homeassistant

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"version": "2026.8.1", "location_name": "Ravenswood"})
        if request.url.path == "/api/states":
            return httpx.Response(200, json=HA_STATES)
        return httpx.Response(404, json={"message": "unknown"})

    monkeypatch.setattr(
        homeassistant,
        "build_client",
        lambda url, token, timeout, verify: httpx.AsyncClient(
            base_url=url, timeout=timeout, transport=httpx.MockTransport(handle)
        ),
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "credentials").mkdir()
    (tmp_path / "credentials" / "homeassistant_house.token").write_text("token", encoding="utf-8")

    config_path = tmp_path / "hub_config.json"
    buttons_path = tmp_path / "buttons.json"
    config_path.write_text(json.dumps(HA_CONFIG), encoding="utf-8")
    buttons_path.write_text(json.dumps(BUTTONS), encoding="utf-8")

    app = create_app(
        HubSettings(config_path=config_path, buttons_path=buttons_path),
        settings_path=tmp_path / "hub_settings.json",
    )
    with TestClient(app) as client:
        yield client


def test_entities_can_be_listed_so_they_can_be_picked(ha_client):
    entities = ha_client.get("/api/devices/house/entities").json()
    by_id = {e["entity_id"]: e for e in entities}

    assert by_id["light.kitchen"]["name"] == "Kitchen"
    assert by_id["switch.amp"]["domain"] == "switch"
    # A thermometer cannot be put on a remote, so it is out of the way by
    # default -- but the backend flagged it rather than hiding it, so the
    # caller can still ask.
    assert "sensor.temperature" not in by_id

    everything = ha_client.get("/api/devices/house/entities?controllable_only=false").json()
    hallway = next(e for e in everything if e["entity_id"] == "sensor.temperature")
    assert hallway["controllable"] is False


def test_only_the_exposed_entities_become_commands(ha_client):
    commands = {c["name"] for c in ha_client.get("/api/devices/house/commands").json()}

    assert "toggle:light.kitchen" in commands
    # Present in the instance, never picked, so never bindable.
    assert not any(name.endswith(":switch.amp") for name in commands)


def test_suggested_bindings_offer_the_plus_minus_keys_for_an_adjustable_light(ha_client):
    suggested = ha_client.get("/api/devices/house/suggested_bindings").json()

    assert suggested["adjust"] == {"consumer_0x0ff0": "up", "consumer_0x0ff1": "down"}
    # The +/- keys are the adjust map's job now, not a fixed command here.
    assert "consumer_0x0ff0" not in suggested["bindings"]


def test_a_backend_with_a_fixed_command_list_has_nothing_to_choose_from(ha_client):
    response = ha_client.get("/api/devices/tv/entities")

    assert response.status_code == 400
    assert "fixed command list" in response.json()["detail"]


def test_listing_entities_of_an_unknown_device_is_a_404(ha_client):
    assert ha_client.get("/api/devices/ghost/entities").status_code == 404


def test_backends_supply_their_own_pairing_words(ha_client):
    """The mechanism generalises; the wording does not."""
    names = {b["name"]: b for b in ha_client.get("/api/backends").json()}

    assert names["homeassistant"]["pair_input_multiline"] is True
    assert "token" in names["homeassistant"]["pair_input_label"].lower()
    assert names["androidtv"]["pair_input_multiline"] is False
    assert names["androidtv"]["pair_input_label"] == "Code"
    # LG webOS pairs with a press on the TV's own remote -- nothing to type
    # back, which is what an empty pair_input_label tells the UI and the CLI.
    assert names["lgtv"]["pairable"] is True
    assert names["lgtv"]["pair_input_label"] == ""
    # A backend that does not pair has no pairing words to offer.
    assert names["virtual"]["pair_label"] == ""


# ---------------------------------------------------------------------------
# A receiver's inputs go through that same picker
#
# The Denon backend's vocabulary is fixed by the model but the useful subset of
# it is chosen, so it reaches the app through the route Home Assistant's
# entities do. That reuse is the entire reason its config key is spelled
# `entities`, and these check it actually arrives without a route of its own.
# ---------------------------------------------------------------------------

AVR_CONFIG = {
    "version": 1,
    "devices": [
        {
            "id": "avr",
            "name": "Receiver",
            "backend": "denon",
            "config": {"host": "10.0.0.7", "entities": ["BD"]},
        }
    ],
    "scenes": [],
    "global_scene": None,
}


@pytest.fixture
def avr_client(tmp_path, monkeypatch):
    """A hub with a Denon device behind a receiver that answers but says nothing."""
    import httpx

    from harmony_hub.backends import denon

    monkeypatch.setattr(
        denon,
        "build_client",
        lambda base_url, timeout: httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=httpx.MockTransport(lambda request: httpx.Response(403, text="")),
        ),
    )

    config_path = tmp_path / "hub_config.json"
    buttons_path = tmp_path / "buttons.json"
    config_path.write_text(json.dumps(AVR_CONFIG), encoding="utf-8")
    buttons_path.write_text(json.dumps(BUTTONS), encoding="utf-8")

    app = create_app(
        HubSettings(config_path=config_path, buttons_path=buttons_path),
        settings_path=tmp_path / "hub_settings.json",
    )
    with TestClient(app) as client:
        yield client


def test_a_receivers_inputs_are_offered_through_the_entity_picker(avr_client):
    found = avr_client.get("/api/devices/avr/entities").json()

    assert {e["domain"] for e in found} == {"input"}
    # The socket on the back marked CBL/SAT is reached by the token SAT/CBL,
    # which is exactly what nobody should have to know to pick it.
    by_name = {e["name"]: e["entity_id"] for e in found}
    assert by_name["CBL/SAT"] == "SAT/CBL"


def test_only_the_picked_inputs_become_commands_on_a_receiver(avr_client):
    commands = {c["name"] for c in avr_client.get("/api/devices/avr/commands").json()}

    assert "input:BD" in commands
    assert "input:PHONO" not in commands
    # The rest of the vocabulary is fixed and always there.
    assert {"volume_up", "power_on", "mute"} <= commands


def test_a_receiver_suggests_the_volume_keys_but_not_the_plus_minus_ones(avr_client):
    suggested = avr_client.get("/api/devices/avr/suggested_bindings").json()

    assert suggested["bindings"]["volume_up"] == "volume_up"
    # Volume has keys of its own; the SmartHome +/- keys stay with the lights.
    assert suggested["adjust"] == {}


def test_a_scene_can_be_activated_and_stopped(client):
    assert client.post("/api/scenes/watch_tv/activate").json()["active_scene"] == "watch_tv"
    assert client.get("/api/state").json()["active_scene"] == "watch_tv"

    assert client.post("/api/scenes/stop").json()["active_scene"] is None


def test_activating_an_unknown_scene_is_a_404(client):
    assert client.post("/api/scenes/ghost/activate").status_code == 404


def test_testing_a_command_reports_success(client):
    response = client.post("/api/devices/tv/test", json={"command": "on"}).json()

    assert response["ok"] is True


def test_testing_an_unknown_command_reports_the_failure_without_a_500(client):
    """Setting a device up means getting commands wrong; that is not a server error."""
    response = client.post("/api/devices/tv/test", json={"command": "nope"}).json()

    assert response["ok"] is False


def test_config_round_trips_through_the_api(client):
    config = client.get("/api/config").json()
    config["scenes"][0]["name"] = "Telly"

    assert client.put("/api/config", json=config).json()["scenes"][0]["name"] == "Telly"
    assert client.get("/api/config").json()["scenes"][0]["name"] == "Telly"
    assert json.loads(client.config_path.read_text())["scenes"][0]["name"] == "Telly"


def test_an_invalid_config_is_rejected_and_the_old_one_survives(client):
    """A bad edit in the UI must not be able to break a running hub."""
    broken = client.get("/api/config").json()
    broken["scenes"][0]["on_start"][0]["device"] = "ghost"

    assert client.put("/api/config", json=broken).status_code == 422
    assert client.get("/api/config").json()["scenes"][0]["on_start"][0]["device"] == "tv"


def test_a_simulated_press_runs_the_real_binding(client):
    """Simulation goes through the engine, so a button that works here really works."""
    client.post("/api/scenes/watch_tv/activate")

    client.post("/api/buttons/volume_up/simulate")

    for _ in range(50):
        if client.get("/api/state").json()["devices"][0]["detail"] != "1 command(s) recorded":
            break
    assert client.get("/api/state").json()["devices"][0]["detail"] == "2 command(s) recorded"


# ---------------------------------------------------------------------------
# Pausing command execution
# ---------------------------------------------------------------------------


def test_hub_state_reports_unpaused_by_default(client):
    assert client.get("/api/state").json()["paused"] is False


def test_pausing_stops_a_simulated_press_from_reaching_the_device(client):
    client.post("/api/scenes/watch_tv/activate")
    baseline = client.get("/api/state").json()["devices"][0]["detail"]

    assert client.post("/api/hub/pause").json() == {"paused": True}
    assert client.get("/api/state").json()["paused"] is True

    client.post("/api/buttons/volume_up/simulate")

    # No polling loop needed here, unlike the unpaused test above: nothing
    # is ever going to arrive, so there is nothing to wait for.
    assert client.get("/api/state").json()["devices"][0]["detail"] == baseline


def test_resuming_lets_the_next_press_through(client):
    client.post("/api/scenes/watch_tv/activate")
    client.post("/api/hub/pause")
    client.post("/api/buttons/volume_up/simulate")

    assert client.post("/api/hub/resume").json() == {"paused": False}
    assert client.get("/api/state").json()["paused"] is False
    client.post("/api/buttons/volume_up/simulate")

    for _ in range(50):
        if client.get("/api/state").json()["devices"][0]["detail"] != "1 command(s) recorded":
            break
    assert client.get("/api/state").json()["devices"][0]["detail"] == "2 command(s) recorded"


# --------------------------------------------------------------------------
# Focus: what the SmartHome +/- keys follow
# --------------------------------------------------------------------------

FOCUS_CONFIG = {
    "version": 1,
    "devices": [
        {
            "id": "amp",
            "name": "Amp",
            "backend": "virtual",
            "config": {
                "commands": ["toggle", "brighter", "dimmer"],
                "focus": {"toggle": ["lamp", "Lamp"]},
                "adjust": {"lamp": {"up": "brighter", "down": "dimmer"}},
            },
        }
    ],
    "scenes": [
        {
            "id": "watch_tv",
            "name": "Watch TV",
            "devices": ["amp"],
            "bindings": {
                "volume_up": {"on_press": [{"type": "device", "device": "amp", "command": "toggle"}]},
                "power": {"on_press": [{"type": "adjust", "direction": "up"}]},
            },
        }
    ],
    "global_scene": None,
}


@pytest.fixture
def focus_client(tmp_path):
    config_path = tmp_path / "hub_config.json"
    buttons_path = tmp_path / "buttons.json"
    config_path.write_text(json.dumps(FOCUS_CONFIG), encoding="utf-8")
    buttons_path.write_text(json.dumps(BUTTONS), encoding="utf-8")

    app = create_app(
        HubSettings(config_path=config_path, buttons_path=buttons_path),
        settings_path=tmp_path / "hub_settings.json",
    )
    with TestClient(app) as client:
        yield client


def test_state_reports_no_focus_until_something_is_touched(focus_client):
    assert focus_client.get("/api/state").json()["focus"] is None


def test_state_reports_the_focus_after_a_press(focus_client):
    focus_client.post("/api/scenes/watch_tv/activate")
    focus_client.post("/api/buttons/volume_up/simulate")

    focus = None
    for _ in range(50):
        focus = focus_client.get("/api/state").json()["focus"]
        if focus is not None:
            break
    assert focus == {"device": "amp", "target": "lamp", "label": "Lamp", "can_adjust": True}


def test_an_adjust_binding_round_trips_through_put_config(focus_client):
    config = focus_client.get("/api/config").json()
    power_binding = config["scenes"][0]["bindings"]["power"]["on_press"][0]
    assert power_binding == {"type": "adjust", "direction": "up", "device": None, "target": None}

    saved = focus_client.put("/api/config", json=config).json()
    assert saved["scenes"][0]["bindings"]["power"]["on_press"][0] == power_binding


def test_the_event_socket_streams_hub_events(client):
    with client.websocket_connect("/api/events") as socket:
        client.post("/api/scenes/watch_tv/activate")

        # A socket opens by replaying recent history, which includes the
        # runtime bringing the hub up, so read past that rather than assuming
        # the scene event is among the first few.
        seen = []
        for _ in range(20):
            seen.append(json.loads(socket.receive_text()))
            if seen[-1]["type"] == "scene":
                break

    assert seen[-1]["scene"] == "watch_tv"


def test_the_socket_replays_recent_history_so_a_new_page_is_not_blank(client):
    client.post("/api/scenes/watch_tv/activate")

    with client.websocket_connect("/api/events") as socket:
        first = json.loads(socket.receive_text())

    assert first["type"] == "hub"  # the runtime starting, before the engine said anything


async def test_the_event_socket_returns_on_a_disconnect_it_was_never_sent_a_reply_to(tmp_path):
    """Regression test for a shutdown hang: uvicorn's graceful shutdown pushes
    a `websocket.disconnect` into the socket's own receive queue and then
    waits -- via polling, not cancellation -- for the handler task to finish
    on its own. A handler that never calls `receive()` never sees that
    message and so never returns, and uvicorn waits forever.

    Driven over a raw ASGI scope rather than `TestClient.websocket_connect`,
    because `TestClient` closes its socket by *cancelling* the handler's
    task outright -- unlike uvicorn, which only delivers the message and
    waits -- so it would pass here even against the old, buggy handler.
    """
    config_path = tmp_path / "hub_config.json"
    buttons_path = tmp_path / "buttons.json"
    config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
    buttons_path.write_text(json.dumps(BUTTONS), encoding="utf-8")
    app = create_app(
        HubSettings(config_path=config_path, buttons_path=buttons_path, autostart=False),
        settings_path=tmp_path / "hub_settings.json",
    )

    messages = [
        {"type": "websocket.connect"},
        {"type": "websocket.disconnect", "code": 1012},
    ]

    async def receive():
        if messages:
            return messages.pop(0)
        await asyncio.sleep(3600)  # nothing else is ever sent in this test

    async def send(message):
        pass

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("test", 123),
        "server": ("test", 80),
        "subprotocols": [],
        "state": {},
    }

    await asyncio.wait_for(app(scope, receive, send), timeout=2.0)


# ---------------------------------------------------------------------------
# Learning buttons
#
# A remote is useless until its signatures have names, and the only place
# those names can come from is a person pressing buttons. These cover the
# writing-down half; the seeing-it half is the live event stream, which
# already publishes an unlearned press under its own hex.
# ---------------------------------------------------------------------------


def test_a_new_button_can_be_named_and_is_persisted(client):
    result = client.post(
        "/api/buttons/learn",
        json={"buttons": [{"key": "menu", "label": "Menu", "signatures": ["c3400000"]}]},
    ).json()

    assert {b["key"] for b in result} == {"volume_up", "power", "menu"}
    # Signatures are stored upper-case, the way the engine looks them up.
    assert next(b for b in result if b["key"] == "menu")["signatures"] == ["C3400000"]
    # And it survives a re-read from disk rather than living only in memory.
    assert {b["key"] for b in client.get("/api/buttons").json()} == {"volume_up", "power", "menu"}


def test_a_learned_button_is_immediately_usable_by_the_engine(client):
    """Naming a button has to reach the running engine, not just the file."""
    client.post(
        "/api/buttons/learn",
        json={"buttons": [{"key": "menu", "label": "Menu", "signatures": ["C3400000"]}]},
    )

    assert client.post("/api/buttons/menu/simulate").json()["signature"] == "C3400000"


def test_a_second_signature_can_join_an_existing_button(client):
    """One physical key can report differently per activity; both are that key."""
    result = client.post(
        "/api/buttons/learn",
        json={"buttons": [{"key": "volume_up", "label": "Volume Up", "signatures": ["C3E90001"]}]},
    ).json()

    assert next(b for b in result if b["key"] == "volume_up")["signatures"] == [
        "C3E90000",
        "C3E90001",
    ]


def test_several_buttons_can_be_named_in_one_go(client):
    result = client.post(
        "/api/buttons/learn",
        json={
            "buttons": [
                {"key": "menu", "label": "Menu", "signatures": ["C3400000"]},
                {"key": "guide", "label": "Guide", "signatures": ["C38D0000"]},
            ]
        },
    ).json()

    assert {"menu", "guide"} <= {b["key"] for b in result}


@pytest.mark.parametrize(
    "bad",
    [
        {"key": "Volume Up", "label": "Volume Up", "signatures": ["C3400000"]},  # spaces
        {"key": "menu", "label": "", "signatures": ["C3400000"]},  # unnamed
        {"key": "menu", "label": "Menu", "signatures": []},  # nothing to identify it by
    ],
)
def test_a_button_that_could_never_be_bound_is_rejected(client, bad):
    """Keys become binding keys, so a malformed one is a binding nothing matches."""
    assert client.post("/api/buttons/learn", json={"buttons": [bad]}).status_code == 422
    assert {b["key"] for b in client.get("/api/buttons").json()} == {"volume_up", "power"}


def test_a_button_can_be_forgotten(client):
    result = client.delete("/api/buttons/power").json()

    assert {b["key"] for b in result} == {"volume_up"}
    assert {b["key"] for b in client.get("/api/buttons").json()} == {"volume_up"}


def test_forgetting_a_button_that_is_not_there_is_a_404(client):
    assert client.delete("/api/buttons/ghost").status_code == 404


def test_buttons_can_be_learned_while_the_hub_is_stopped(client):
    """Naming what was already captured needs no radio."""
    client.post("/api/hub/stop")

    response = client.post(
        "/api/buttons/learn",
        json={"buttons": [{"key": "menu", "label": "Menu", "signatures": ["C3400000"]}]},
    )

    assert response.status_code == 200
    assert "menu" in {b["key"] for b in response.json()}


# ---------------------------------------------------------------------------
# The hub can be down and the API still up
#
# This is the point of the runtime layer. Every case below used to stop
# uvicorn serving at all, which meant the settings page that could explain
# the problem was the page that failed to load.
# ---------------------------------------------------------------------------


@pytest.fixture
def broken_client(tmp_path):
    """A hub told to use a radio it has no address for -- it cannot start."""
    config_path = tmp_path / "hub_config.json"
    buttons_path = tmp_path / "buttons.json"
    config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
    buttons_path.write_text(json.dumps(BUTTONS), encoding="utf-8")

    app = create_app(
        HubSettings(config_path=config_path, buttons_path=buttons_path, source="radio"),
        settings_path=tmp_path / "hub_settings.json",
    )
    with TestClient(app) as client:
        client.config_path = config_path
        yield client


def test_a_hub_that_cannot_start_still_serves_its_api(broken_client):
    state = broken_client.get("/api/state").json()

    assert state["hub"]["state"] == "failed"
    assert "no remote address" in state["hub"]["detail"]
    assert state["hub"]["problems"] == ["Source is 'radio' but no remote address is set."]
    # And everything needed to fix it is readable.
    assert broken_client.get("/api/config").json()["scenes"][0]["id"] == "watch_tv"
    assert broken_client.get("/api/settings").json()["source"] == "radio"


def test_version_answers_even_while_the_hub_itself_is_failed(broken_client):
    """`/api/version` says nothing about `HubRuntime` at all -- a `failed` hub must not look like a bad deploy."""
    response = broken_client.get("/api/version")
    assert response.status_code == 200
    assert response.json()["deployed"] is False


def test_a_broken_hub_can_be_repaired_over_the_api_and_started(broken_client):
    """The whole loop: diagnose, correct, restart -- without touching a terminal."""
    settings = broken_client.get("/api/settings").json()
    settings["source"] = "none"

    status = broken_client.put("/api/settings?restart=true", json=settings).json()

    assert status["state"] == "running"
    assert broken_client.post("/api/scenes/watch_tv/activate").status_code == 200


def test_configuration_is_editable_while_the_hub_is_down(broken_client):
    """A hub that will not start is exactly when its configuration needs changing."""
    config = broken_client.get("/api/config").json()
    config["scenes"][0]["name"] = "Telly"

    assert broken_client.put("/api/config", json=config).status_code == 200
    assert json.loads(broken_client.config_path.read_text())["scenes"][0]["name"] == "Telly"


def test_a_config_file_that_will_not_parse_still_serves(tmp_path):
    config_path = tmp_path / "hub_config.json"
    config_path.write_text("{ broken", encoding="utf-8")
    (tmp_path / "buttons.json").write_text(json.dumps(BUTTONS), encoding="utf-8")

    app = create_app(
        HubSettings(config_path=config_path, buttons_path=tmp_path / "buttons.json"),
        settings_path=tmp_path / "hub_settings.json",
    )
    with TestClient(app) as client:
        state = client.get("/api/state").json()

        assert state["hub"]["state"] == "running"
        assert "could not be read" in state["hub"]["config_error"]

        # Saving the empty stand-in over it would delete whatever the file
        # really holds, so replacing it has to be asked for.
        assert client.put("/api/config", json={"version": 1}).status_code == 409
        assert client.put("/api/config?force=true", json={"version": 1}).status_code == 200


# ---------------------------------------------------------------------------
# Lifecycle over HTTP
# ---------------------------------------------------------------------------


def test_the_hub_can_be_stopped_and_started_without_restarting_the_server(client):
    assert client.post("/api/hub/stop").json()["state"] == "stopped"

    # The API is entirely alive; only the parts that need an engine refuse.
    assert client.get("/api/state").json()["hub"]["state"] == "stopped"
    assert client.get("/api/config").status_code == 200
    assert client.post("/api/scenes/watch_tv/activate").status_code == 409
    assert client.post("/api/buttons/volume_up/simulate").status_code == 409
    assert client.get("/api/devices/tv/commands").status_code == 409
    assert client.post("/api/hub/pause").status_code == 409
    assert client.post("/api/hub/resume").status_code == 409

    assert client.post("/api/hub/start").json()["state"] == "running"
    assert client.post("/api/scenes/watch_tv/activate").status_code == 200


def test_a_stopped_hub_reports_its_devices_as_stopped_rather_than_missing(client):
    client.post("/api/hub/stop")

    devices = client.get("/api/state").json()["devices"]

    assert devices[0]["running"] is False
    assert devices[0]["detail"] == "the hub is stopped"


def test_restarting_keeps_the_configuration_and_the_event_log(client):
    before = len(client.get("/api/state").json()["scenes"])

    assert client.post("/api/hub/restart").json()["state"] == "running"

    assert len(client.get("/api/state").json()["scenes"]) == before
    with client.websocket_connect("/api/events") as socket:
        assert json.loads(socket.receive_text())["type"] == "hub"


def test_autostart_off_serves_the_page_with_the_hub_waiting(tmp_path):
    """For an install that must be configured before it touches any equipment."""
    (tmp_path / "hub_config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    (tmp_path / "buttons.json").write_text(json.dumps(BUTTONS), encoding="utf-8")

    app = create_app(
        HubSettings(
            config_path=tmp_path / "hub_config.json",
            buttons_path=tmp_path / "buttons.json",
            autostart=False,
        ),
        settings_path=tmp_path / "hub_settings.json",
    )
    with TestClient(app) as client:
        assert client.get("/api/state").json()["hub"]["state"] == "stopped"
        assert client.post("/api/hub/start").json()["state"] == "running"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_round_trip_and_persist(client, tmp_path):
    settings = client.get("/api/settings").json()
    settings["replay_speed"] = 2.0

    assert client.put("/api/settings", json=settings).status_code == 200
    assert client.get("/api/settings").json()["replay_speed"] == 2.0


def test_an_invalid_setting_is_rejected_and_the_old_one_survives(client):
    settings = client.get("/api/settings").json()
    settings["address"] = "not-hex"

    assert client.put("/api/settings", json=settings).status_code == 422
    assert client.get("/api/settings").json()["address"] is None


def test_a_bind_change_is_saved_but_flagged_as_needing_a_restart(client):
    """Moving the live listener would take this page's URL with it."""
    settings = client.get("/api/settings").json()
    settings["port"] = 9999

    status = client.put("/api/settings", json=settings).json()

    assert status["pending_restart"] is True
    assert status["port"] == 8765  # where this process is still actually listening


# ---------------------------------------------------------------------------
# Checks and dry-run
# ---------------------------------------------------------------------------


def test_checks_report_what_is_working(client):
    checks = {c["name"]: c for c in client.get("/api/checks").json()}

    assert checks["Configuration file"]["ok"] is True
    assert "2 button(s)" in checks["Button map"]["detail"]
    assert checks["Event source"]["ok"] is True
    # Nothing was built into this test tree, and saying so is the point:
    # today a missing UI build is only a log line at startup.
    assert checks["Web interface"]["ok"] is False
    assert checks["Device: TV"]["ok"] is True


def test_checks_notice_a_stopped_hub(client):
    client.post("/api/hub/stop")

    checks = {c["name"]: c for c in client.get("/api/checks").json()}

    assert checks["Device: TV"]["detail"] == "not started -- the hub is stopped"


def test_trying_settings_reports_a_missing_capture_without_saving_them(client, tmp_path):
    candidate = client.get("/api/settings").json()
    candidate["source"] = "replay"
    candidate["replay_path"] = str(tmp_path / "gone.jsonl")

    checks = {c["name"]: c for c in client.post("/api/settings/try", json=candidate).json()}

    assert checks["Event source"]["ok"] is False
    assert "no capture file at" in checks["Event source"]["detail"]
    # Nothing was committed: the hub is still on its original source.
    assert client.get("/api/settings").json()["source"] == "none"


def test_trying_workable_settings_opens_the_source_for_real(client, tmp_path):
    capture = tmp_path / "cap.jsonl"
    capture.write_text(json.dumps({"type": "packet", "t": 0.0, "raw": "00" * 10}) + "\n", encoding="utf-8")
    candidate = client.get("/api/settings").json()
    candidate["source"] = "replay"
    candidate["replay_path"] = str(capture)

    checks = {c["name"]: c for c in client.post("/api/settings/try", json=candidate).json()}

    assert checks["Settings"]["ok"] is True
    assert checks["Event source"]["ok"] is True
    assert "1 packet(s)" in checks["Event source"]["detail"]


# ---------------------------------------------------------------------------
# Address discovery
# ---------------------------------------------------------------------------


def test_discovery_starts_idle(client):
    assert client.get("/api/radio/discover").json()["state"] == "idle"


def test_discovery_reports_a_missing_radio_rather_than_a_500(client):
    """No FT232H is attached in CI, and that has to be a message, not a crash."""
    assert client.post("/api/radio/discover").status_code == 200

    for _ in range(200):
        status = client.get("/api/radio/discover").json()
        if status["state"] != "running":
            break

    assert status["state"] == "failed"
    assert status["detail"]


def test_discovery_defaults_to_the_hub_method(client):
    assert client.post("/api/radio/discover").json()["method"] == "hub"


def test_sniff_discovery_reports_a_missing_radio_rather_than_a_500(client):
    """The hub-less method needs the same radio the hub method does -- same failure, same message."""
    started = client.post("/api/radio/discover?method=sniff")
    assert started.status_code == 200
    assert started.json()["method"] == "sniff"

    for _ in range(200):
        status = client.get("/api/radio/discover").json()
        if status["state"] != "running":
            break

    assert status["state"] == "failed"
    assert status["detail"]


# ---------------------------------------------------------------------------
# Serving the built web UI
# ---------------------------------------------------------------------------


def test_ui_entry_points_are_served_without_a_browser_cache(tmp_path):
    """A remote update swaps the UI on disk; a browser that cached index.html forever would never notice."""
    static_dir = tmp_path / "web"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (static_dir / "main.dart.js").write_text("// app", encoding="utf-8")
    assets_dir = static_dir / "assets"
    assets_dir.mkdir()
    (assets_dir / "picture.png").write_bytes(b"\x89PNG")

    app = create_app(HubSettings(config_path=tmp_path / "hub_config.json", buttons_path=tmp_path / "buttons.json"), static_dir=static_dir)
    with TestClient(app) as client:
        assert client.get("/").headers["cache-control"] == "no-cache"
        assert client.get("/main.dart.js").headers["cache-control"] == "no-cache"
        assert "cache-control" not in {k.lower() for k in client.get("/assets/picture.png").headers}
