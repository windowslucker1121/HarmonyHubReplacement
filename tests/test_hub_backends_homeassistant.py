"""The Home Assistant backend, driven against a stand-in for a real instance.

Nothing here talks to a Home Assistant. What is worth testing without one is
the behaviour that is easy to get wrong and only shows up in the living room:
that an instance which is down still lets its bindings be edited, that a token
is checked before it is kept and never written into configuration, that an
entity nobody exposed cannot be commanded by accident, and that the remote's
button keys and the generated command names have not drifted apart.

The fake is an `httpx.MockTransport`, substituted through `build_client` --
the same seam the Android TV suite uses to replace its client.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from harmony_hub import backends
from harmony_hub.backends import homeassistant
from harmony_hub.backends.homeassistant import HomeAssistantBackend

REPO_ROOT = Path(__file__).resolve().parents[1]

CONFIG_BODY = {"version": "2026.8.1", "location_name": "Ravenswood"}

STATES = [
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
    {"entity_id": "light.sofa", "state": "off", "attributes": {"friendly_name": "Sofa Lamp"}},
    {"entity_id": "switch.amp", "state": "off", "attributes": {"friendly_name": "Amplifier"}},
    {"entity_id": "switch.fan", "state": "off", "attributes": {}},
    {"entity_id": "scene.movie_night", "state": "unknown", "attributes": {"friendly_name": "Movie Night"}},
    {"entity_id": "script.goodnight", "state": "off", "attributes": {"friendly_name": "Goodnight"}},
    {"entity_id": "media_player.den", "state": "playing",
     "attributes": {"friendly_name": "Den", "is_volume_muted": False}},
    {"entity_id": "sensor.temperature", "state": "19.4", "attributes": {"friendly_name": "Hallway"}},
]


class FakeHomeAssistant:
    """Answers the handful of endpoints the backend uses, and records posts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.requests: list[httpx.Request] = []
        #: Set to a status code to make every request answer with it, or to an
        #: exception instance to make every request raise.
        self.fail_with: int | Exception | None = None
        self.states = [dict(state) for state in STATES]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.fail_with, Exception):
            raise self.fail_with
        if isinstance(self.fail_with, int):
            return httpx.Response(self.fail_with, json={"message": "no"})

        path = request.url.path
        if path == "/api/config":
            return httpx.Response(200, json=CONFIG_BODY)
        if path == "/api/states":
            return httpx.Response(200, json=self.states)
        if path.startswith("/api/states/"):
            entity_id = path.removeprefix("/api/states/")
            found = next((s for s in self.states if s["entity_id"] == entity_id), None)
            if found is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=found)
        if path.startswith("/api/services/"):
            domain, service = path.removeprefix("/api/services/").split("/", 1)
            self.calls.append((f"{domain}.{service}", json.loads(request.content or b"{}")))
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "unknown"})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Substitutes the transport, and keeps the token file inside tmp_path."""
    instance = FakeHomeAssistant()

    def build(url: str, token: str, timeout: float, verify: bool) -> httpx.AsyncClient:
        instance.tokens_seen = getattr(instance, "tokens_seen", [])
        instance.tokens_seen.append(token)
        return httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=instance.transport(),
        )

    monkeypatch.setattr(homeassistant, "build_client", build)
    monkeypatch.chdir(tmp_path)
    return instance


def make(config=None) -> HomeAssistantBackend:
    return HomeAssistantBackend("ha", {"url": "http://ha.local:8123", **(config or {})})


def with_token(config=None, token="a-long-lived-token") -> HomeAssistantBackend:
    """A backend whose token file already exists, as it would after pairing."""
    backend = make(config)
    backend._write_token(token)
    return backend


# --------------------------------------------------------------------------
# Registration and configuration
# --------------------------------------------------------------------------


def test_the_backend_is_registered_and_pairable():
    assert backends.get("homeassistant") is HomeAssistantBackend
    assert issubclass(HomeAssistantBackend, backends.Pairable)


def test_the_config_schema_asks_for_an_address_but_never_a_token():
    schema = HomeAssistantBackend.config_schema()
    assert schema["required"] == ["url"]
    # The whole point of routing the token through pairing is that it is not
    # a field anyone can type into, and not something GET /api/config returns.
    assert "token" not in schema["properties"]
    assert "entities" in schema["properties"]


def test_pairing_copy_is_overridden_because_there_is_no_code_on_a_screen():
    assert HomeAssistantBackend.pair_input_multiline is True
    assert "token" in HomeAssistantBackend.pair_input_label.lower()
    assert HomeAssistantBackend.pair_label != backends.Pairable.pair_label


# --------------------------------------------------------------------------
# Starting up
# --------------------------------------------------------------------------


async def test_connect_with_no_address_says_so_instead_of_raising(fake):
    backend = HomeAssistantBackend("ha", {})
    await backend.connect()
    assert backend._state == "unconfigured"
    assert (await backend.health()).ok is False


async def test_connect_without_a_token_starts_anyway_so_it_can_be_paired(fake):
    # The engine only registers a backend whose connect() returned, and the
    # pairing routes only reach a registered backend. Raising here would make
    # an unpaired device impossible to ever pair.
    backend = make()
    await backend.connect()
    assert backend._state == "unpaired"
    assert "token" in backend._detail


async def test_connect_reaches_the_instance_and_remembers_what_it_is(fake):
    backend = with_token()
    await backend.connect()
    assert backend._state == "connected"

    health = await backend.health()
    assert health.ok is True
    assert "Ravenswood" in health.detail
    assert "2026.8.1" in health.detail


async def test_an_instance_that_is_down_does_not_stop_the_hub(fake):
    fake.fail_with = httpx.ConnectError("no route to host")
    backend = with_token()
    await backend.connect()
    assert backend._state == "unreachable"
    assert (await backend.health()).ok is False


async def test_a_rejected_token_reports_as_unpaired_rather_than_unreachable(fake):
    # Retrying cannot fix a bad token, and "cannot reach it" would send the
    # user to look at the network instead of at their token.
    fake.fail_with = 401
    backend = with_token()
    await backend.connect()
    assert backend._state == "unpaired"


async def test_health_is_cached_so_polling_the_device_list_is_not_a_load(fake):
    # Every GET /api/state polls this. Without the cache -- and without
    # connect()'s own probe seeding it -- a device list left open would put
    # two requests per poll on the Home Assistant box forever.
    backend = with_token()
    await backend.connect()
    before = len(fake.requests)
    for _ in range(5):
        await backend.health()
    assert len(fake.requests) == before

    backend._health_at -= homeassistant.HEALTH_TTL + 1
    await backend.health()
    assert len(fake.requests) > before


async def test_health_names_entities_that_have_vanished_from_home_assistant(fake):
    # Renaming an entity in Home Assistant breaks every binding pointing at
    # it, and nothing else in the system would notice until a button press.
    backend = with_token({"entities": ["light.kitchen", "light.deleted", "switch.gone"]})
    await backend.connect()
    health = await backend.health()
    assert health.ok is False
    assert "light.deleted" in health.detail
    assert "switch.gone" in health.detail


async def test_an_unread_state_list_is_not_the_same_as_a_deleted_entity(fake):
    """Emptiness cannot tell an absent answer from an empty instance.

    Conflating them would report every binding as broken the moment one
    request timed out, which is a far worse lie than saying nothing.
    """
    backend = with_token({"entities": ["light.kitchen"]})

    # /api/config answers but /api/states does not: nothing to compare against.
    await backend.connect()
    backend._names, backend._names_read = {}, False
    assert backend._describe().ok is True

    # /api/states answered, and the entity genuinely is not in it.
    backend._names, backend._names_read = {"light.other": "Other"}, True
    described = backend._describe()
    assert described.ok is False
    assert "light.kitchen" in described.detail


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def test_only_the_chosen_entities_become_commands(fake):
    backend = with_token({"entities": ["light.kitchen", "switch.amp"]})
    await backend.connect()
    names = {c.name for c in await backend.commands()}

    assert "toggle:light.kitchen" in names
    assert "brighter:light.kitchen" in names
    assert "toggle:switch.amp" in names
    # Present in the fake instance, never picked, so never offered.
    assert not any(name.endswith(":media_player.den") for name in names)
    assert not any(name.endswith(":sensor.temperature") for name in names)


async def test_commands_work_with_home_assistant_unreachable(fake):
    """Editing a binding must not require the equipment to be switched on."""
    fake.fail_with = httpx.ConnectError("down")
    backend = with_token({"entities": ["light.kitchen"]})
    await backend.connect()

    commands = await backend.commands()
    assert "toggle:light.kitchen" in {c.name for c in commands}
    # No friendly name was ever fetched, so it falls back to the entity id.
    assert any(c.label.startswith("Kitchen") for c in commands)


async def test_labels_use_friendly_names_and_disambiguate_scenes(fake):
    backend = with_token({"entities": ["light.sofa", "scene.movie_night"]})
    await backend.connect()
    labels = {c.name: c.label for c in await backend.commands()}

    assert labels["toggle:light.sofa"] == "Sofa Lamp — Toggle"
    # The hub has scenes of its own, and they appear in the same dropdown.
    assert labels["activate:scene.movie_night"] == "Scene: Movie Night — Activate"


async def test_the_verbs_offered_follow_the_domain(fake):
    backend = with_token(
        {"entities": ["scene.movie_night", "script.goodnight", "media_player.den"]}
    )
    await backend.connect()
    names = {c.name for c in await backend.commands()}

    assert names >= {
        "activate:scene.movie_night",
        "run:script.goodnight",
        "stop:script.goodnight",
        "play_pause:media_player.den",
        "mute:media_player.den",
    }
    # A scene cannot be dimmed, and a light cannot be played.
    assert "brighter:scene.movie_night" not in names


async def test_only_the_commands_worth_holding_are_repeatable(fake):
    backend = with_token({"entities": ["light.kitchen", "media_player.den"]})
    await backend.connect()
    repeatable = {c.name for c in await backend.commands() if c.repeatable}

    assert repeatable == {
        "brighter:light.kitchen",
        "dimmer:light.kitchen",
        "volume_up:media_player.den",
        "volume_down:media_player.den",
    }


async def test_a_custom_action_is_offered_alongside_the_entity_commands(fake):
    backend = with_token(
        {
            "entities": ["light.kitchen"],
            "actions": [
                {"name": "warm", "label": "Warm lights", "service": "light.turn_on",
                 "target": {"entity_id": ["light.kitchen"]},
                 "data": {"brightness_pct": 30, "kelvin": 2700}},
            ],
        }
    )
    await backend.connect()
    labels = {c.name: c.label for c in await backend.commands()}
    assert labels["warm"] == "Warm lights"


async def test_a_custom_action_named_like_an_entity_command_is_refused(fake):
    """A colon in the name could never be told apart from `verb:entity_id`."""
    backend = with_token(
        {"actions": [{"name": "toggle:light.kitchen", "service": "light.turn_on"}]}
    )
    await backend.connect()
    assert await backend.commands() == []


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


async def test_toggling_a_light_calls_the_universal_service(fake):
    backend = with_token({"entities": ["light.kitchen"]})
    await backend.connect()
    await backend.send("toggle:light.kitchen")
    assert fake.calls == [("homeassistant.toggle", {"entity_id": "light.kitchen"})]


async def test_brighter_and_dimmer_step_a_light_thats_already_on(fake):
    # Relative stepping is what lets this work without reading the current
    # brightness first, which is what makes it safe to fire on every repeat.
    backend = with_token({"entities": ["light.kitchen"]})
    await backend.connect()
    await backend.send("brighter:light.kitchen")
    await backend.send("dimmer:light.kitchen")

    assert fake.calls == [
        ("light.turn_on", {"entity_id": "light.kitchen", "brightness_step_pct": 10}),
        ("light.turn_on", {"entity_id": "light.kitchen", "brightness_step_pct": -10}),
    ]


async def test_brighter_turns_on_an_off_light_instead_of_stepping_it(fake):
    # `brightness_step_pct` has nothing to step from on an off light, so
    # stepping "up" from off means switching it on at a fixed level instead.
    backend = with_token({"entities": ["light.sofa"]})
    await backend.connect()
    await backend.send("brighter:light.sofa")

    assert fake.calls == [
        (
            "light.turn_on",
            {"entity_id": "light.sofa", "brightness_pct": homeassistant.BRIGHTNESS_TURN_ON_PCT},
        ),
    ]


async def test_dimmer_does_not_turn_on_an_off_light(fake):
    # Only "up" has anything special to do with an off light; stepping "down"
    # on one that is already off is left exactly as relative as ever.
    backend = with_token({"entities": ["light.sofa"]})
    await backend.connect()
    await backend.send("dimmer:light.sofa")

    assert fake.calls == [
        ("light.turn_on", {"entity_id": "light.sofa", "brightness_step_pct": -10}),
    ]


async def test_activating_a_home_assistant_scene(fake):
    backend = with_token({"entities": ["scene.movie_night"]})
    await backend.connect()
    await backend.send("activate:scene.movie_night")
    assert fake.calls == [("scene.turn_on", {"entity_id": "scene.movie_night"})]


async def test_mute_reads_the_player_first_because_there_is_no_toggle_service(fake):
    backend = with_token({"entities": ["media_player.den"]})
    await backend.connect()

    await backend.send("mute:media_player.den")
    assert fake.calls == [
        ("media_player.volume_mute", {"entity_id": "media_player.den", "is_volume_muted": True}),
    ]

    # And back again once Home Assistant reports it muted.
    fake.calls.clear()
    for state in fake.states:
        if state["entity_id"] == "media_player.den":
            state["attributes"]["is_volume_muted"] = True
    await backend.send("mute:media_player.den")
    assert fake.calls == [
        ("media_player.volume_mute", {"entity_id": "media_player.den", "is_volume_muted": False}),
    ]


async def test_a_custom_action_sends_its_target_and_data_together(fake):
    backend = with_token(
        {
            "actions": [
                {"name": "warm", "service": "light.turn_on",
                 "target": {"entity_id": ["light.kitchen"]},
                 "data": {"brightness_pct": 30, "kelvin": 2700}},
            ]
        }
    )
    await backend.connect()
    await backend.send("warm")
    assert fake.calls == [
        ("light.turn_on",
         {"brightness_pct": 30, "kelvin": 2700, "entity_id": ["light.kitchen"]}),
    ]


async def test_commanding_an_entity_nobody_exposed_is_refused(fake):
    # Reachable when an entity is dropped from the picker but a binding still
    # points at it. Failing loudly here is why exposure is explicit at all.
    backend = with_token({"entities": ["light.kitchen"]})
    await backend.connect()
    with pytest.raises(backends.BackendError, match="not exposed"):
        await backend.send("toggle:light.sofa")
    assert fake.calls == []


async def test_a_verb_the_domain_does_not_have_is_refused(fake):
    backend = with_token({"entities": ["scene.movie_night"]})
    await backend.connect()
    with pytest.raises(backends.BackendError, match="no 'brighter' command"):
        await backend.send("brighter:scene.movie_night")


async def test_a_command_in_the_wrong_shape_says_what_the_shape_is(fake):
    backend = with_token({"entities": ["light.kitchen"]})
    await backend.connect()
    with pytest.raises(backends.BackendError, match="verb:entity_id"):
        await backend.send("turn_on")


async def test_a_refused_service_call_becomes_a_backend_error(fake):
    backend = with_token({"entities": ["light.kitchen"]})
    await backend.connect()
    fake.fail_with = 400
    with pytest.raises(backends.BackendError, match="400"):
        await backend.send("toggle:light.kitchen")


async def test_a_token_revoked_while_running_is_reported_as_unpaired(fake):
    backend = with_token({"entities": ["light.kitchen"]})
    await backend.connect()
    fake.fail_with = 401
    with pytest.raises(backends.BackendError):
        await backend.send("toggle:light.kitchen")
    assert backend._state == "unpaired"


async def test_a_call_that_works_clears_a_stale_unreachable_state(fake):
    backend = with_token({"entities": ["light.kitchen"]})
    await backend.connect()
    backend._set_state("unreachable", "was down a moment ago")
    await backend.send("toggle:light.kitchen")
    assert backend._state == "connected"


# --------------------------------------------------------------------------
# The entity catalogue
# --------------------------------------------------------------------------


async def test_the_catalogue_flags_read_only_domains_rather_than_hiding_them(fake):
    backend = with_token()
    await backend.connect()
    catalogue = {entry["entity_id"]: entry for entry in await backend.entities()}

    assert catalogue["light.kitchen"]["controllable"] is True
    assert catalogue["sensor.temperature"]["controllable"] is False
    assert catalogue["light.kitchen"]["name"] == "Kitchen"
    # No friendly name in Home Assistant, so one is made from the entity id.
    assert catalogue["switch.fan"]["name"] == "Fan"


async def test_the_catalogue_reports_a_failure_instead_of_an_empty_list(fake):
    backend = with_token()
    await backend.connect()
    fake.fail_with = httpx.ConnectError("down")
    with pytest.raises(backends.BackendError, match="could not list entities"):
        await backend.entities()


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------


async def test_pairing_keeps_the_token_out_of_the_configuration(fake, tmp_path):
    backend = make({"entities": ["light.kitchen"]})
    await backend.connect()
    assert backend._state == "unpaired"

    assert "profile/security" in await backend.pair_start()
    await backend.pair_finish("  a-long-lived-token  ")

    assert backend._state == "connected"
    # On disk, in the gitignored credentials directory -- and nowhere in the
    # device config, which any LAN client can read back over the API.
    token_file = tmp_path / "credentials" / "homeassistant_ha.token"
    assert token_file.read_text(encoding="utf-8").strip() == "a-long-lived-token"
    assert "token" not in json.dumps(backend.config)


async def test_a_token_home_assistant_rejects_is_never_written(fake, tmp_path):
    # Home Assistant shows a token once, so a bad one has to be reported while
    # it is still on the user's screen -- not stored and discovered at press time.
    fake.fail_with = 401
    backend = make()
    await backend.connect()
    with pytest.raises(backends.BackendError, match="did not accept"):
        await backend.pair_finish("wrong")
    assert not (tmp_path / "credentials" / "homeassistant_ha.token").exists()


async def test_pairing_needs_an_address_first(fake):
    backend = HomeAssistantBackend("ha", {})
    await backend.connect()
    with pytest.raises(backends.BackendError, match="no address"):
        await backend.pair_start()


async def test_an_empty_token_is_refused_without_a_round_trip(fake):
    backend = make()
    await backend.connect()
    with pytest.raises(backends.BackendError, match="no token"):
        await backend.pair_finish("   ")


# --------------------------------------------------------------------------
# The two vocabularies
# --------------------------------------------------------------------------


def test_suggested_bindings_point_the_smarthome_keys_at_what_was_exposed():
    backend = HomeAssistantBackend(
        "ha",
        {
            "url": "http://ha.local:8123",
            "entities": ["light.kitchen", "light.sofa", "switch.amp", "switch.fan"],
        },
    )
    suggested = backend.suggested_bindings()

    assert suggested["consumer_0x0ff2"] == "toggle:light.kitchen"   # bulb, upper
    assert suggested["consumer_0x0ff3"] == "toggle:light.sofa"      # bulb, lower
    assert suggested["consumer_0x0ff4"] == "toggle:switch.amp"      # socket, upper
    assert suggested["consumer_0x0ff5"] == "toggle:switch.fan"      # socket, lower
    # The +/- keys are `suggested_adjust`'s job, not a fixed command here.
    assert "consumer_0x0ff0" not in suggested
    assert "consumer_0x0ff1" not in suggested


def test_suggested_bindings_offer_nothing_when_nothing_is_exposed():
    assert HomeAssistantBackend("ha", {"url": "http://ha.local:8123"}).suggested_bindings() == {}


def test_suggested_adjust_offers_the_plus_minus_keys_when_something_can_be_stepped():
    backend = HomeAssistantBackend("ha", {"url": "http://ha.local:8123", "entities": ["light.kitchen"]})
    assert backend.suggested_adjust() == {"consumer_0x0ff0": "up", "consumer_0x0ff1": "down"}


def test_suggested_adjust_offers_nothing_when_nothing_exposed_can_be_stepped():
    # A switch can be toggled but not stepped, so the +/- keys stay free for
    # something else rather than suggesting a key that always reports
    # "nothing to turn up".
    backend = HomeAssistantBackend("ha", {"url": "http://ha.local:8123", "entities": ["switch.amp"]})
    assert backend.suggested_adjust() == {}


# --------------------------------------------------------------------------
# Focus: what the +/- keys remember after a command
# --------------------------------------------------------------------------


def test_focus_for_names_the_entity_behind_any_recognised_command():
    backend = HomeAssistantBackend(
        "ha", {"url": "http://ha.local:8123", "entities": ["light.kitchen", "switch.amp"]}
    )
    backend._names = {"light.kitchen": "Kitchen", "switch.amp": "Amplifier"}

    # A switch takes the focus just as much as a light -- "the last thing
    # touched" includes something that cannot be stepped, on purpose.
    assert backend.focus_for("toggle:switch.amp") == backends.FocusTarget("switch.amp", "Amplifier")
    assert backend.focus_for("turn_on:light.kitchen") == backends.FocusTarget("light.kitchen", "Kitchen")


def test_focus_for_falls_back_to_a_titled_name_before_any_probe():
    backend = HomeAssistantBackend("ha", {"url": "http://ha.local:8123", "entities": ["light.kitchen"]})
    assert backend.focus_for("toggle:light.kitchen") == backends.FocusTarget("light.kitchen", "Kitchen")


def test_focus_for_ignores_custom_actions_and_unexposed_entities():
    backend = HomeAssistantBackend(
        "ha",
        {
            "url": "http://ha.local:8123",
            "entities": ["light.kitchen"],
            "actions": [{"name": "warm", "service": "light.turn_on"}],
        },
    )
    assert backend.focus_for("warm") is None
    assert backend.focus_for("bare_name") is None
    assert backend.focus_for("toggle:light.unexposed") is None


def test_adjust_command_finds_the_stepping_verb_for_the_domain():
    backend = HomeAssistantBackend(
        "ha",
        {"url": "http://ha.local:8123", "entities": ["light.kitchen", "media_player.den", "switch.amp"]},
    )
    assert backend.adjust_command("light.kitchen", "up") == "brighter:light.kitchen"
    assert backend.adjust_command("light.kitchen", "down") == "dimmer:light.kitchen"
    assert backend.adjust_command("media_player.den", "up") == "volume_up:media_player.den"
    assert backend.adjust_command("media_player.den", "down") == "volume_down:media_player.den"


def test_adjust_command_is_none_for_something_that_cannot_be_stepped():
    backend = HomeAssistantBackend("ha", {"url": "http://ha.local:8123", "entities": ["switch.amp"]})
    assert backend.adjust_command("switch.amp", "up") is None


def test_adjust_command_is_none_once_the_entity_is_no_longer_exposed():
    # Reachable when an entity is dropped from the picker but the focus still
    # names it -- the same "not exposed" guard `send` applies.
    backend = HomeAssistantBackend("ha", {"url": "http://ha.local:8123", "entities": []})
    assert backend.adjust_command("light.kitchen", "up") is None


def test_every_suggested_button_exists_on_the_real_remote():
    """The two vocabularies must not drift apart.

    Same check the Android TV suite makes, for the same reason: a suggestion
    naming a button the remote does not have is silently dropped by the
    mapper, so nothing would ever report it.
    """
    known = set(json.loads((REPO_ROOT / "buttons.json").read_text(encoding="utf-8")))
    backend = HomeAssistantBackend(
        "ha",
        {
            "url": "http://ha.local:8123",
            "entities": ["light.kitchen", "light.sofa", "switch.amp", "switch.fan"],
        },
    )
    unknown = set(backend.suggested_bindings()) - known
    assert not unknown, f"suggested bindings name buttons the remote does not have: {unknown}"


async def test_every_suggested_command_is_one_the_backend_offers(fake):
    backend = with_token({"entities": ["light.kitchen", "light.sofa", "switch.amp", "switch.fan"]})
    await backend.connect()
    offered = {c.name for c in await backend.commands()}
    assert set(backend.suggested_bindings().values()) <= offered
