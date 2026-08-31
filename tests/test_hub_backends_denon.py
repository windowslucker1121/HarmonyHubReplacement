"""The Denon backend, driven against a stand-in for a real receiver.

Nothing here talks to a receiver. What is worth testing without one is the
behaviour that is easy to get wrong and only shows up in the living room: that
a receiver in standby still lets its bindings be edited, that both transports
put the same bytes on the wire so switching between them cannot change what a
button does, that the socket marked CBL/SAT is reached by the token `SAT/CBL`
rather than by its label, and that the remote's button keys and the generated
command names have not drifted apart.

There are two fakes because there are two transports: an `httpx.MockTransport`
substituted through `build_client`, and a stand-in for `asyncio.open_connection`
that answers Denon's queries the way a receiver on port 23 does.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from harmony_hub import backends
from harmony_hub.backends import denon
from harmony_hub.backends.denon import DenonBackend

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeReceiver:
    """A receiver reachable over HTTP, recording what it was told."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.state = {"Power": "ON", "Mute": "off", "InputFuncSelect": "MPLAY"}
        #: Set to a status code to make every request answer with it, or to an
        #: exception instance to make every request raise.
        self.fail_with: int | Exception | None = None
        #: Answered for the status document alone, for the newer models that
        #: take commands happily but refuse to say what they are doing.
        self.status_code = 200

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if isinstance(self.fail_with, Exception):
            raise self.fail_with
        if isinstance(self.fail_with, int):
            return httpx.Response(self.fail_with, text="no")

        if request.url.path == denon.STATUS_PATH:
            if self.status_code != 200:
                return httpx.Response(self.status_code, text="")
            body = "".join(f"<{k}><value>{v}</value></{k}>" for k, v in self.state.items())
            return httpx.Response(200, text=f"<item>{body}</item>")
        if request.url.path == denon.COMMAND_PATH:
            self.sent.append(unquote(request.url.query.decode()))
            return httpx.Response(200, text="")
        return httpx.Response(404, text="")


class FakeTelnetReceiver:
    """A receiver reachable on port 23, answering queries the way Denon does."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.state = {"PW": "ON", "MU": "OFF", "SI": "MPLAY"}
        #: Unrelated status the receiver volunteers before answering, which a
        #: real one does constantly and a reader has to read past.
        self.noise: list[str] = []
        self.fail_with: Exception | None = None
        #: How many times a connection was opened. The receiver allows exactly
        #: one at a time, so this is the number that matters.
        self.connections = 0
        self.closed = 0

    async def open(self, host: str, port: int):
        self.connections += 1
        if self.fail_with is not None:
            raise self.fail_with
        reader = _Reader()
        return reader, _Writer(self, reader)


class _Reader:
    def __init__(self) -> None:
        self._lines: list[bytes] = []

    def feed(self, lines: list[str]) -> None:
        self._lines.extend(line.encode("ascii") + b"\r" for line in lines)

    async def readuntil(self, separator: bytes) -> bytes:
        if not self._lines:
            # A real receiver would simply go quiet; the reader's own timeout
            # is what ends the wait, and waiting it out in a test is a waste.
            raise asyncio.IncompleteReadError(b"", None)
        return self._lines.pop(0)


class _Writer:
    def __init__(self, receiver: FakeTelnetReceiver, reader: _Reader) -> None:
        self._receiver = receiver
        self._reader = reader

    def write(self, data: bytes) -> None:
        command = data.decode("ascii")
        assert command.endswith("\r"), "Denon terminates every command with a carriage return"
        command = command.rstrip("\r")
        self._receiver.sent.append(command)
        if command.endswith("?"):
            value = self._receiver.state.get(command[:-1])
            answer = [f"{command[:-1]}{value}"] if value is not None else []
            self._reader.feed(self._receiver.noise + answer)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._receiver.closed += 1

    async def wait_closed(self) -> None:
        pass


@pytest.fixture
def fake(monkeypatch):
    """A receiver on HTTP, with the inter-command gap taken out of the way."""
    receiver = FakeReceiver()
    monkeypatch.setattr(
        denon,
        "build_client",
        lambda base_url, timeout: httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=receiver.transport()
        ),
    )
    monkeypatch.setattr(denon, "MIN_COMMAND_GAP", 0.0)
    return receiver


@pytest.fixture
def telnet(monkeypatch):
    """A receiver on port 23, with the inter-command gap taken out of the way."""
    receiver = FakeTelnetReceiver()
    monkeypatch.setattr(denon.asyncio, "open_connection", receiver.open)
    monkeypatch.setattr(denon, "MIN_COMMAND_GAP", 0.0)
    return receiver


def make(config=None) -> DenonBackend:
    return DenonBackend("avr", {"host": "10.0.0.7", **(config or {})})


def over_telnet(config=None) -> DenonBackend:
    return make({"transport": "telnet", **(config or {})})


# --------------------------------------------------------------------------
# Registration and configuration
# --------------------------------------------------------------------------


def test_the_backend_registers_under_its_own_name():
    assert backends.available()["denon"] is DenonBackend


def test_the_device_form_only_asks_for_things_it_can_render():
    # The form renders object and array properties as a JSON textarea and
    # everything else as a real field, so a nested object here would be a
    # receiver configured by typing braces.
    kinds = {p["type"] for p in DenonBackend.config_schema()["properties"].values()}

    assert kinds <= {"string", "integer", "number", "boolean", "array"}


def test_the_inputs_are_offered_through_the_shared_entity_picker():
    # Named `entities` on purpose: the picker, its API route and its detection
    # of ids that have gone stale are all keyed on that name rather than on a
    # backend, so calling it `inputs` would mean writing all three again.
    assert "entities" in DenonBackend.config_schema()["properties"]
    assert hasattr(DenonBackend, "entities")


def test_anything_other_than_telnet_is_treated_as_http():
    # A hand-edited configuration is the likely source of a typo here, and
    # guessing telnet would silently take the receiver's only connection.
    assert make({"transport": "nonsense"}).transport == "http"
    assert make().transport == "http"
    assert make({"transport": "telnet"}).transport == "telnet"


# --------------------------------------------------------------------------
# Starting up
# --------------------------------------------------------------------------


async def test_a_device_with_no_address_starts_anyway_and_says_so(fake):
    backend = DenonBackend("avr", {"host": ""})
    await backend.connect()

    health = await backend.health()
    assert not health.ok
    assert "no address" in health.detail


async def test_a_receiver_in_standby_does_not_stop_the_hub(fake):
    # Network Control left on its default means the receiver leaves the network
    # entirely in standby. That is the normal evening state of the thing, not a
    # configuration error, and connect() must not raise over it.
    fake.fail_with = httpx.ConnectError("no route to host")
    backend = make()

    await backend.connect()

    assert not (await backend.health()).ok


async def test_a_receiver_that_is_down_still_lets_its_bindings_be_edited(fake):
    fake.fail_with = httpx.ConnectError("no route to host")
    backend = make({"entities": ["BD"]})
    await backend.connect()

    assert "input:BD" in {c.name for c in await backend.commands()}


async def test_closing_a_device_that_never_started_is_harmless(fake):
    backend = make()

    await backend.close()
    await backend.close()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def test_the_command_list_needs_no_receiver():
    # Built from configuration rather than from the unit, so a scene can be
    # mapped with the receiver switched off at the wall.
    assert len(await make().commands()) > 30


async def test_only_the_picked_inputs_become_commands(fake):
    backend = make({"entities": ["BD", "SAT/CBL"]})

    inputs = [c for c in await backend.commands() if c.name.startswith("input:")]

    assert [c.name for c in inputs] == ["input:BD", "input:SAT/CBL"]
    assert [c.label for c in inputs] == ["Input — Blu-ray", "Input — CBL/SAT"]


async def test_an_input_is_labelled_by_its_socket_not_its_protocol_token(fake):
    # The socket on the back marked CBL/SAT answers to `SAT/CBL`. Nobody should
    # ever have to know that, which is the entire reason for the picker.
    backend = make({"entities": ["SAT/CBL"]})

    command = next(c for c in await backend.commands() if c.name == "input:SAT/CBL")

    assert command.label == "Input — CBL/SAT"
    assert command.description == "SISAT/CBL"


def test_every_suggested_button_exists_on_the_real_remote():
    """The two vocabularies must not drift apart.

    A suggestion naming a button the remote does not have is silently dropped
    by the mapper, so the mistake shows up as a key that mysteriously does
    nothing rather than as an error anywhere.
    """
    known = set(json.loads((REPO_ROOT / "buttons.json").read_text(encoding="utf-8")))

    unknown = set(DenonBackend.suggested_bindings(make())) - known
    assert not unknown, f"suggested bindings name buttons the remote does not have: {unknown}"


async def test_every_suggested_command_is_one_the_backend_offers():
    offered = {c.name for c in await make().commands()}

    assert set(make().suggested_bindings().values()) <= offered


async def test_the_repeatable_commands_are_the_ones_safe_to_hold():
    by_name = {c.name: c for c in await make().commands()}

    assert by_name["volume_up"].repeatable
    assert by_name["cursor_down"].repeatable
    # Firing this forty times a second would fight the receiver rather than
    # ramp anything.
    assert not by_name["power_on"].repeatable
    assert not by_name["surround_movie"].repeatable


def test_the_receiver_never_steals_the_smarthome_keys():
    # Volume has keys of its own. Taking the focus here would point the +/-
    # keys at the amplifier every time a scene touched it, which is not what
    # somebody who just dimmed a light expects them to do next.
    backend = make({"entities": ["BD"]})

    assert backend.focus_for("volume_up") is None
    assert backend.suggested_adjust() == {}


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


async def test_a_command_becomes_the_query_string_on_its_own(fake):
    backend = make()
    await backend.connect()

    await backend.send("volume_up")

    assert fake.sent == ["MVUP"]


async def test_a_command_containing_a_space_survives_the_url(fake):
    backend = make()
    await backend.connect()

    await backend.send("surround_pure_direct")

    assert fake.sent == ["MSPURE DIRECT"]


async def test_selecting_an_input_sends_the_token_rather_than_the_label(fake):
    backend = make({"entities": ["SAT/CBL"]})
    await backend.connect()

    await backend.send("input:SAT/CBL")

    assert fake.sent == ["SISAT/CBL"]


async def test_an_input_nobody_picked_is_refused(fake):
    backend = make({"entities": ["BD"]})
    await backend.connect()

    with pytest.raises(backends.BackendError, match="Choose entities"):
        await backend.send("input:PHONO")
    assert fake.sent == []


async def test_an_unknown_command_is_an_error_rather_than_a_silent_no_op(fake):
    backend = make()
    await backend.connect()

    with pytest.raises(backends.BackendError, match="no command 'teleport'"):
        await backend.send("teleport")


async def test_a_receiver_that_refuses_a_command_points_at_the_other_transport(fake):
    # This is the failure the whole transport switch exists for: some firmware
    # simply does not serve this endpoint, and the useful thing to say is what
    # to try next rather than the status code on its own.
    backend = make()
    await backend.connect()
    fake.fail_with = 403

    with pytest.raises(backends.BackendError, match="telnet"):
        await backend.send("volume_up")


async def test_a_receiver_that_cannot_be_reached_says_so(fake):
    backend = make()
    await backend.connect()
    fake.fail_with = httpx.ConnectError("no route to host")

    with pytest.raises(backends.BackendError, match="MVUP failed"):
        await backend.send("volume_up")


# --------------------------------------------------------------------------
# The two transports
# --------------------------------------------------------------------------


async def test_both_transports_put_the_same_bytes_on_the_wire(fake, telnet):
    """Switching transport must never change what a button does.

    The command table is written once for exactly this reason; a divergence
    here would mean a binding that worked over HTTP quietly doing something
    else after somebody changed a dropdown.
    """
    over_http = make({"entities": ["BD"]})
    await over_http.connect()
    direct = over_telnet({"entities": ["BD"]})
    await direct.connect()
    telnet.sent.clear()

    for command in ("volume_up", "surround_pure_direct", "menu_on", "input:BD"):
        await over_http.send(command)
        await direct.send(command)

    assert fake.sent == telnet.sent == ["MVUP", "MSPURE DIRECT", "MNMEN ON", "SIBD"]


async def test_telnet_does_not_hold_the_receivers_only_connection(telnet):
    # A receiver takes one telnet client at a time. A socket held for the
    # evening is a socket the Denon app and Home Assistant cannot have.
    backend = over_telnet()
    await backend.connect()
    opened = telnet.connections

    await backend.send("volume_up")
    await backend.send("volume_down")

    assert telnet.connections == opened + 2
    assert telnet.closed == telnet.connections


async def test_a_reply_is_found_past_whatever_the_receiver_volunteers(telnet):
    # A receiver narrates unrelated state changes down the same socket, so the
    # answer to a question is rarely the first line back.
    telnet.noise = ["MV45", "PSBAS 50", "SSVCTZMAON"]
    backend = over_telnet()
    await backend.connect()

    assert (await backend.health()).detail.startswith("on")


async def test_a_receiver_that_is_not_listening_reports_the_reason(telnet):
    telnet.fail_with = ConnectionRefusedError()
    backend = over_telnet()
    await backend.connect()

    with pytest.raises(backends.BackendError, match="ConnectionRefusedError"):
        await backend.send("volume_up")


async def test_commands_are_spaced_out_so_the_receiver_does_not_drop_them(monkeypatch, fake):
    # Denon asks for 50ms between commands and ignores what arrives sooner,
    # which on a held volume key would show up as a ramp that skips steps.
    monkeypatch.setattr(denon, "MIN_COMMAND_GAP", 0.05)
    backend = make()
    await backend.connect()

    started = time.monotonic()
    await backend.send("volume_up")
    await backend.send("volume_up")
    await backend.send("volume_up")

    assert time.monotonic() - started >= 0.1


# --------------------------------------------------------------------------
# Commands that carry a number
# --------------------------------------------------------------------------


async def test_setting_a_volume_pads_it_to_the_width_the_protocol_expects(fake):
    backend = make()
    await backend.connect()

    await backend.send("volume", {"level": 45})
    await backend.send("volume", {"level": 5})

    assert fake.sent == ["MV45", "MV05"]


async def test_a_volume_outside_the_range_never_reaches_the_wire(fake):
    # The digit count is the command here: `MV1000` is not a rejected volume,
    # it is a different instruction.
    backend = make()
    await backend.connect()

    with pytest.raises(backends.BackendError, match="between 0 and 98"):
        await backend.send("volume", {"level": 1000})
    with pytest.raises(backends.BackendError, match="between 0 and 98"):
        await backend.send("volume", {})
    assert fake.sent == []


async def test_a_sleep_timer_pads_to_three_digits(fake):
    backend = make()
    await backend.connect()

    await backend.send("sleep", {"minutes": 30})

    assert fake.sent == ["SLP030"]


# --------------------------------------------------------------------------
# Mute, which the protocol has no toggle for
# --------------------------------------------------------------------------


async def test_the_mute_toggle_reads_the_receiver_rather_than_guessing(fake):
    fake.state["Mute"] = "off"
    backend = make()
    await backend.connect()

    await backend.send("mute")

    assert fake.sent == ["MUON"]


async def test_the_mute_toggle_unmutes_a_receiver_that_is_already_muted(fake):
    fake.state["Mute"] = "on"
    backend = make()
    await backend.connect()

    await backend.send("mute")

    assert fake.sent == ["MUOFF"]


async def test_the_mute_toggle_asks_the_receiver_over_telnet_too(telnet):
    telnet.state["MU"] = "ON"
    backend = over_telnet()
    await backend.connect()
    telnet.sent.clear()

    await backend.send("mute")

    assert "MU?" in telnet.sent
    assert telnet.sent[-1] == "MUOFF"


# --------------------------------------------------------------------------
# Reading state back, for a scene's conditions
# --------------------------------------------------------------------------


async def test_readable_offers_power_source_and_mute_over_http(fake):
    backend = make()
    await backend.connect()

    targets = {t.target for t in await backend.readable()}

    assert targets == {"power", "source", "muted"}


async def test_readable_adds_surround_only_over_telnet(telnet):
    backend = over_telnet()
    await backend.connect()

    targets = {t.target for t in await backend.readable()}

    assert "surround" in targets


async def test_read_state_reports_power_source_and_mute(fake):
    fake.state["Power"] = "ON"
    fake.state["InputFuncSelect"] = "SAT/CBL"
    fake.state["Mute"] = "on"
    backend = make()
    await backend.connect()

    assert await backend.read_state("power") == "on"
    assert await backend.read_state("source") == "SAT/CBL"
    assert await backend.read_state("muted") == "true"


async def test_read_state_reports_standby_and_unmuted(fake):
    fake.state["Power"] = "STANDBY"
    fake.state["Mute"] = "off"
    backend = make()
    await backend.connect()

    assert await backend.read_state("power") == "standby"
    assert await backend.read_state("muted") == "false"


async def test_read_state_of_an_unknown_target_is_rejected(fake):
    backend = make()
    await backend.connect()

    with pytest.raises(backends.BackendError, match="no state 'ghost'"):
        await backend.read_state("ghost")


async def test_the_surround_mode_reads_over_telnet(telnet):
    # "MS" is a query prefix like any other in READABLE, just not bundled
    # into it -- the fake's state dict works the same way either way.
    telnet.state["MS"] = "NEURAL:X"
    backend = over_telnet()
    await backend.connect()

    assert await backend.read_state("surround") == "NEURAL:X"


async def test_the_surround_mode_reads_past_whatever_else_the_receiver_volunteers(telnet):
    telnet.state["MS"] = "DOLBY DIGITAL"
    telnet.noise = ["PSDRC OFF", "PSLFE 00", "PSBAS 56"]
    backend = over_telnet()
    await backend.connect()

    assert await backend.read_state("surround") == "DOLBY DIGITAL"


async def test_the_surround_mode_cannot_be_read_over_http(fake):
    """Confirmed against a real AVR-X2700H: the command endpoint answers
    `200` with an empty body for `MS?`, the same as it does here -- not an
    HTTP error, just nothing worth reading. `readable()` already leaves
    `surround` off the list for HTTP, but `read_state` has to fail honestly
    too, for a saved condition built while still on telnet that outlives a
    later switch to HTTP.
    """
    backend = make()
    await backend.connect()

    with pytest.raises(backends.BackendError, match="could not read"):
        await backend.read_state("surround")

    assert "MS?" in fake.sent


async def test_the_surround_mode_is_unreadable_when_nothing_answers(telnet):
    # No "MS" key in telnet.state -- the receiver stays silent, the same as
    # a query it does not recognise.
    backend = over_telnet()
    await backend.connect()

    with pytest.raises(backends.BackendError, match="could not read"):
        await backend.read_state("surround")


async def test_a_receiver_that_will_not_say_points_at_the_discrete_commands(fake):
    # Tracking the state here instead would go wrong the first time somebody
    # picked up the receiver's own remote, so the honest answer is to name the
    # two commands that always work.
    fake.status_code = 403
    backend = make()
    await backend.connect()

    with pytest.raises(backends.BackendError, match="'Mute' or 'Unmute'"):
        await backend.send("mute")


# --------------------------------------------------------------------------
# The input catalogue
# --------------------------------------------------------------------------


async def test_the_inputs_can_be_picked_with_the_receiver_switched_off(fake):
    # Somebody setting this up for the first time is very likely to be doing it
    # with the receiver in standby, and a picker that needed it awake would be
    # useless at exactly that moment.
    fake.fail_with = httpx.ConnectError("no route to host")
    backend = make()
    await backend.connect()

    found = await backend.entities()

    assert len(found) == len(denon.INPUTS)
    assert {e["domain"] for e in found} == {"input"}


async def test_the_current_input_is_marked_when_the_receiver_is_awake(fake):
    fake.state["InputFuncSelect"] = "BD"
    backend = make()
    await backend.connect()

    selected = [e["entity_id"] for e in await backend.entities() if e["state"]]

    assert selected == ["BD"]


async def test_the_catalogue_offers_the_token_and_shows_the_label(fake):
    backend = make()
    await backend.connect()

    entry = next(e for e in await backend.entities() if e["name"] == "CBL/SAT")

    assert entry["entity_id"] == "SAT/CBL"


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


async def test_health_reports_what_the_receiver_is_doing(fake):
    fake.state.update({"Power": "ON", "InputFuncSelect": "BD", "Mute": "on"})
    backend = make({"entities": ["BD"]})
    await backend.connect()

    health = await backend.health()

    assert health.ok
    assert health.detail == "on · Blu-ray · muted · 1 input"


async def test_health_reports_standby_as_a_normal_state(fake):
    fake.state["Power"] = "STANDBY"
    backend = make()
    await backend.connect()

    health = await backend.health()

    assert health.ok
    assert health.detail.startswith("standby")


async def test_health_is_cached_so_polling_the_device_list_is_not_a_load(fake):
    backend = make()
    await backend.connect()
    fake.fail_with = httpx.ConnectError("no route to host")

    # The probe connect() just did is still fresh, so this must not go asking
    # again -- and the cached answer is the one from before the receiver died.
    assert (await backend.health()).ok


async def test_a_receiver_that_answers_but_will_not_describe_itself_is_healthy(fake):
    # Newer firmware drops the status document while still taking commands
    # perfectly well. Reporting that as broken would be a lie about a receiver
    # that works.
    fake.status_code = 403
    backend = make()
    await backend.connect()

    health = await backend.health()

    assert health.ok
    assert "reachable" in health.detail


async def test_an_unreachable_receiver_is_reported_as_unreachable(fake):
    fake.fail_with = httpx.ConnectError("no route to host")
    backend = make()
    await backend.connect()

    health = await backend.health()

    assert not health.ok
    assert "cannot reach" in health.detail


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _description_xml(manufacturer: str, friendly_name: str = "", model_name: str = "") -> str:
    fields = f"<manufacturer>{manufacturer}</manufacturer>"
    if friendly_name:
        fields += f"<friendlyName>{friendly_name}</friendlyName>"
    if model_name:
        fields += f"<modelName>{model_name}</modelName>"
    return f"<root><device>{fields}</device></root>"


def _found_at(monkeypatch, locations: list[str]) -> None:
    """Makes SSDP itself a no-op: `discover()` proceeds straight to fetching
    these `LOCATION` URLs, the same seam `build_client` is for the transport."""

    async def fake_search(timeout: float) -> list[str]:
        return locations

    monkeypatch.setattr(denon, "_ssdp_search", fake_search)


def _serving(monkeypatch, pages: dict) -> None:
    """Answers each absolute URL discovery fetches with fixed status and body."""

    def handle(request: httpx.Request) -> httpx.Response:
        status, body = pages.get(str(request.url), (404, ""))
        return httpx.Response(status, text=body)

    monkeypatch.setattr(
        denon,
        "build_client",
        lambda base_url, timeout: httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=httpx.MockTransport(handle)
        ),
    )


async def test_a_receiver_is_found_and_named_by_its_friendly_name(monkeypatch):
    _found_at(monkeypatch, ["http://10.0.0.7:8080/description.xml"])
    _serving(
        monkeypatch,
        {
            "http://10.0.0.7:8080/description.xml": (
                200,
                _description_xml("Denon", friendly_name="Living Room AVR", model_name="AVR-X2700H"),
            )
        },
    )

    assert await denon.discover() == [
        {"name": "Living Room AVR", "host": "10.0.0.7", "version": "AVR-X2700H"}
    ]


async def test_a_device_named_by_model_when_it_has_no_friendly_name(monkeypatch):
    _found_at(monkeypatch, ["http://10.0.0.7:8080/description.xml"])
    _serving(
        monkeypatch,
        {
            "http://10.0.0.7:8080/description.xml": (
                200,
                _description_xml("Marantz", model_name="SR7015"),
            )
        },
    )

    found = await denon.discover()

    assert found == [{"name": "SR7015", "host": "10.0.0.7", "version": "SR7015"}]


async def test_a_device_from_another_manufacturer_is_not_listed(monkeypatch):
    _found_at(monkeypatch, ["http://10.0.0.9:1400/xml/device_description.xml"])
    _serving(
        monkeypatch,
        {
            "http://10.0.0.9:1400/xml/device_description.xml": (
                200,
                _description_xml("Sonos, Inc.", friendly_name="Living Room Sonos"),
            )
        },
    )

    assert await denon.discover() == []


async def test_several_locations_on_one_host_collapse_to_one_entry(monkeypatch):
    # Newer firmware answers two of Denon's three advertised device types
    # from the same box, at two different description URLs.
    _found_at(
        monkeypatch,
        [
            "http://10.0.0.7:8080/description.xml",
            "http://10.0.0.7:60006/upnp/desc/aios_device/aios_device.xml",
        ],
    )
    _serving(
        monkeypatch,
        {
            "http://10.0.0.7:8080/description.xml": (
                200,
                _description_xml("Denon", friendly_name="Living Room AVR"),
            ),
            "http://10.0.0.7:60006/upnp/desc/aios_device/aios_device.xml": (
                200,
                _description_xml("Denon", friendly_name="Living Room AVR (AIOS)"),
            ),
        },
    )

    found = await denon.discover()

    assert len(found) == 1


async def test_the_port_in_location_never_becomes_the_host(monkeypatch):
    # LOCATION carries the description server's own port -- 60006 on newer
    # firmware -- which has nothing to do with the control ports (8080/23)
    # the backend actually talks to.
    _found_at(monkeypatch, ["http://10.0.0.7:60006/upnp/desc/aios_device/aios_device.xml"])
    _serving(
        monkeypatch,
        {
            "http://10.0.0.7:60006/upnp/desc/aios_device/aios_device.xml": (
                200,
                _description_xml("Denon", friendly_name="Living Room AVR"),
            )
        },
    )

    found = await denon.discover()

    assert found[0]["host"] == "10.0.0.7"


async def test_a_description_that_will_not_answer_is_skipped_not_fatal(monkeypatch):
    _found_at(
        monkeypatch,
        ["http://10.0.0.8:8080/description.xml", "http://10.0.0.7:8080/description.xml"],
    )
    _serving(
        monkeypatch,
        {
            "http://10.0.0.8:8080/description.xml": (404, ""),
            "http://10.0.0.7:8080/description.xml": (
                200,
                _description_xml("Marantz", friendly_name="Study Receiver"),
            ),
        },
    )

    found = await denon.discover()

    assert [entry["name"] for entry in found] == ["Study Receiver"]


async def test_nothing_answering_ssdp_is_an_empty_list_not_an_error(monkeypatch):
    _found_at(monkeypatch, [])

    assert await denon.discover() == []


async def test_a_network_with_no_usable_interface_is_an_empty_list_not_an_error(monkeypatch):
    async def raises(timeout: float):
        raise OSError("no route to host")

    monkeypatch.setattr(denon, "_ssdp_search", raises)

    assert await denon.discover() == []


def test_an_m_search_request_has_the_headers_and_blank_line_the_protocol_requires():
    request = denon._msearch("urn:schemas-upnp-org:device:MediaRenderer:1")

    assert request.startswith(b"M-SEARCH * HTTP/1.1\r\n")
    assert b"MAN: " + b'"ssdp:discover"' + b"\r\n" in request
    assert b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n" in request
    # The blank line ending the request is not optional: a receiver drops a
    # malformed M-SEARCH silently rather than rejecting it.
    assert request.endswith(b"\r\n\r\n")
