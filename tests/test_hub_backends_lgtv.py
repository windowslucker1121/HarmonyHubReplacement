"""The LG webOS TV backend, driven against a stand-in for aiowebostv's client.

Nothing here talks to a television. What is worth testing without one is the
behaviour that is easy to get wrong and expensive to discover in the living
room: that an unpaired device never opens a socket at all (which is what
would put a prompt on the TV's screen for nobody to answer), that a stale
key is reported rather than retried forever, that Wake-on-LAN is what
`power_on` actually sends, and that pairing keeps re-showing the TV's prompt
rather than giving up after one short window.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harmony_hub import backends
from harmony_hub.backends import _ssdp, lgtv
from harmony_hub.backends.lgtv import LgTvBackend

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeTvState:
    def __init__(self) -> None:
        self.is_on = True
        self.current_app_id = None
        self.apps: dict = {}
        self.volume = None


class FakeWebOsClient:
    """Stands in for `aiowebostv.WebOsClient`, recording what it was asked to send."""

    #: What the next `connect()` should do: None to succeed, or an exception
    #: instance/class to raise.
    fail_with: BaseException | type[BaseException] | None = None
    #: What `get_connection_info()` hands back, for the MAC auto-detect test.
    connection_info: dict = {}

    def __init__(self, host: str, client_key: str | None = None) -> None:
        self.host = host
        self.client_key = client_key
        self._connected = False
        self.tv_state = FakeTvState()
        self.sent: list[tuple] = []
        self.state_callbacks: list = []
        self.inputs: list[dict] = []
        self.apps: list[dict] = []

    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        if type(self).fail_with is not None:
            raise type(self).fail_with
        self._connected = True
        if self.client_key is None:
            self.client_key = "new-client-key"
        return True

    async def disconnect(self) -> None:
        self._connected = False

    async def register_state_update_callback(self, callback) -> None:
        self.state_callbacks.append(callback)

    async def get_connection_info(self) -> dict:
        return type(self).connection_info

    async def get_inputs(self) -> list[dict]:
        return self.inputs

    async def get_apps(self) -> list[dict]:
        return self.apps

    async def button(self, name: str) -> None:
        self.sent.append(("button", name))

    async def power_off(self) -> None:
        self.sent.append(("power_off",))

    async def set_screen_state(self, on: bool) -> None:
        self.sent.append(("set_screen_state", on))

    async def set_volume(self, level: int) -> None:
        self.sent.append(("set_volume", level))

    async def set_channel(self, channel: str) -> None:
        self.sent.append(("set_channel", channel))

    async def set_input(self, input_id: str) -> None:
        self.sent.append(("set_input", input_id))

    async def launch_app(self, app: str) -> None:
        self.sent.append(("launch_app", app))

    async def change_sound_output(self, output: str) -> None:
        self.sent.append(("change_sound_output", output))

    async def send_message(self, message: str) -> None:
        self.sent.append(("send_message", message))

    async def play(self) -> None:
        self.sent.append(("play",))


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Substitutes the client, keeps keys inside tmp_path, and skips the
    real inter-attempt sleep in the pairing retry loop."""
    FakeWebOsClient.fail_with = None
    FakeWebOsClient.connection_info = {}
    monkeypatch.setattr(lgtv, "WebOsClient", FakeWebOsClient)
    monkeypatch.setattr(lgtv, "PAIR_RETRY_GAP", 0.0)
    monkeypatch.chdir(tmp_path)
    yield FakeWebOsClient
    FakeWebOsClient.fail_with = None
    FakeWebOsClient.connection_info = {}


def make(config=None) -> LgTvBackend:
    return LgTvBackend("lounge", {"host": "10.0.0.9", **(config or {})})


# --------------------------------------------------------------------------
# Registration and the command table
# --------------------------------------------------------------------------


def test_the_backend_is_registered_and_pairable():
    assert backends.get("lgtv") is LgTvBackend
    assert issubclass(LgTvBackend, backends.Pairable)


def test_pairing_has_nothing_to_type_back():
    # webOS pairs with a press on the TV's own remote, not a code -- an
    # empty pair_input_label is how the UI and the CLI know to skip the text
    # field entirely rather than asking for a "Code" nobody has.
    assert LgTvBackend.pair_input_label == ""


def test_the_device_form_asks_for_an_address():
    schema = LgTvBackend.config_schema()

    assert schema["required"] == ["host"]
    assert schema["properties"]["host"]["title"] == "Address"
    # The form renders object and array properties as a JSON textarea and
    # everything else as a real field, so a nested object here would be a
    # TV configured by typing braces.
    kinds = {p["type"] for p in schema["properties"].values()}
    assert kinds <= {"string", "integer", "number", "boolean", "array"}


def test_the_inputs_and_apps_are_offered_through_the_shared_entity_picker():
    # Named `entities` on purpose: the picker, its API route and its
    # detection of ids that have gone stale are all keyed on that name.
    assert "entities" in LgTvBackend.config_schema()["properties"]
    assert hasattr(LgTvBackend, "entities")


async def test_commands_cover_the_remote_and_mark_what_may_repeat():
    commands = await make().commands()
    names = {c.name for c in commands}

    assert {"dpad_up", "select", "back", "home", "mute", "volume_up", "digit_0"} <= names
    assert {
        "power_on",
        "power_off",
        "screen_on",
        "screen_off",
        "set_volume",
        "set_channel",
        "set_input",
        "launch_app",
        "sound_output",
        "toast",
        "button",
    } <= names

    repeatable = {c.name for c in commands if c.repeatable}
    assert {"volume_up", "dpad_down", "channel_up", "rewind"} <= repeatable
    # Repeating these while a button is held would be actively wrong.
    assert not repeatable & {"power_on", "power_off", "select", "mute"}


def test_every_button_name_is_unique_across_the_fixed_table():
    names = [name for name, *_ in lgtv._BUTTONS]
    assert len(names) == len(set(names))


def test_the_suggested_mapping_joins_two_real_vocabularies():
    """Keeps `buttons.json` and the command table from drifting apart."""
    buttons = set(json.loads((REPO_ROOT / "buttons.json").read_text(encoding="utf-8")))
    commands = {c.name for c in lgtv.COMMANDS}

    assert set(lgtv.SUGGESTED_BINDINGS) <= buttons
    assert set(lgtv.SUGGESTED_BINDINGS.values()) <= commands
    assert make().suggested_bindings() == lgtv.SUGGESTED_BINDINGS


def test_focus_for_never_takes_the_focus():
    # Pressing Volume Up on the TV must not steal the SmartHome +/- keys
    # away from whatever light was last touched.
    assert make().focus_for("volume_up") is None


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


async def test_an_unpaired_device_starts_without_ever_prompting_the_tv(fake):
    """No stored key must mean `connect()` never builds a client at all --
    doing so would send registration, which is what puts the TV's own
    accept/deny prompt on screen. That is fine once, deliberately, from
    `pair_start()`; doing it on every ordinary hub startup and retry would
    flash a prompt at an empty room."""
    device = make()
    await device.connect()

    health = await device.health()
    assert not health.ok
    assert "not paired" in health.detail
    assert device._client is None, "a client was built although no key exists yet"
    assert device._retry is None, "retrying cannot fix a missing pairing"

    await device.close()


async def test_connecting_with_a_stored_key_reaches_the_device(fake):
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()

    health = await device.health()
    assert health.ok
    assert health.detail == "on"
    # The disconnect-detection callback actually got registered -- easy to
    # get wrong, since the library's own method is async.
    assert device._client.state_callbacks == [device._on_state_update]

    await device.close()


async def test_a_rejected_or_revoked_pairing_is_reported_and_not_retried(fake):
    fake.fail_with = lgtv.WebOsTvPairError("no longer trusted")
    device = make()
    device._save_key_file(client_key="stale-key")
    await device.connect()

    health = await device.health()
    assert not health.ok
    assert "pair it again" in health.detail
    assert device._retry is None, "retrying cannot fix a key the TV no longer accepts"

    await device.close()


async def test_an_unreachable_device_starts_and_keeps_trying(fake):
    fake.fail_with = OSError("no route to host")
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()

    health = await device.health()
    assert not health.ok
    assert "cannot reach 10.0.0.9" in health.detail
    assert device._retry is not None and not device._retry.done()

    await device.close()
    assert device._retry is None


async def test_a_device_with_no_address_says_so_instead_of_failing(fake):
    device = LgTvBackend("lounge", {})
    await device.connect()

    assert (await device.health()).detail == "no address set"


async def test_a_dropped_connection_is_noticed_and_retried(fake):
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()
    client = device._client

    client._connected = False
    client.tv_state.is_on = False
    await device._on_state_update(client.tv_state)

    health = await device.health()
    assert not health.ok
    assert "lost the connection" in health.detail
    assert device._retry is not None and not device._retry.done()

    await device.close()


async def test_closing_disconnects_and_forgets_the_client(fake):
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()
    client = device._client

    await device.close()

    assert not client.is_connected()
    assert device._client is None


# --------------------------------------------------------------------------
# Wake-on-LAN
# --------------------------------------------------------------------------


#: What a real webOS TV answers `connectionmanager/getinfo` with -- captured
#: from an OLED65G29LA on wifi. Both interfaces report a MAC and neither says
#: which one is live, which is the whole reason `power_on` wakes both.
REAL_CONNECTION_INFO = {
    "wifiInfo": {"macAddress": "4C:BC:E9:D7:F6:2A"},
    "wiredInfo": {"macAddress": "74:E6:B8:6A:AF:E0"},
    "p2pInfo": {"macAddress": "4E:BC:E9:D7:F6:2A"},
}


@pytest.fixture
def packets(monkeypatch):
    """Collects what `power_on` would have put on the wire."""
    sent: list[tuple] = []
    monkeypatch.setattr(
        lgtv,
        "_send_magic_packets",
        lambda macs, port, broadcast: sent.append((list(macs), port, broadcast)),
    )
    return sent


async def test_power_on_sends_a_magic_packet_not_an_ssap_call(fake, packets):
    device = make(config={"mac": "aa:bb:cc:dd:ee:ff"})
    await device.send("power_on", {})

    assert packets == [(["AA:BB:CC:DD:EE:FF"], lgtv.DEFAULT_WOL_PORT, lgtv.DEFAULT_WOL_BROADCAST)]
    # No client needed -- this is the one command that must work on a TV
    # that is completely off, paired or not.
    assert device._client is None


async def test_power_on_wakes_every_interface_the_tv_reported(fake, packets):
    """The regression that matters.

    A TV on wifi still reports a MAC for its empty Ethernet socket, and
    `getinfo` says nothing about which one is carrying traffic. An earlier
    version preferred `wiredInfo`, so every magic packet went to a port with
    no cable in it and the TV never woke -- silently, because sending a
    packet nothing listens to looks exactly like success.
    """
    fake.connection_info = REAL_CONNECTION_INFO
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()
    await device.close()

    await device.send("power_on", {})

    (macs, _port, _broadcast) = packets[0]
    assert "4C:BC:E9:D7:F6:2A" in macs, "the wifi interface -- the live one here"
    assert "74:E6:B8:6A:AF:E0" in macs, "the wired interface, woken too since we cannot tell"
    # Wi-Fi Direct wakes nothing and would just be a third pointless packet.
    assert "4E:BC:E9:D7:F6:2A" not in macs


async def test_power_on_without_a_mac_says_so_instead_of_silently_doing_nothing(fake):
    device = make()
    with pytest.raises(backends.BackendError, match="no MAC address"):
        await device.send("power_on", {})


def test_a_malformed_mac_is_rejected_before_anything_is_sent():
    with pytest.raises(backends.BackendError, match="does not look like a MAC address"):
        lgtv._send_magic_packets(
            ["not-a-mac"], lgtv.DEFAULT_WOL_PORT, lgtv.DEFAULT_WOL_BROADCAST
        )


def test_one_bad_mac_stops_the_whole_send_rather_than_half_of_it():
    # Building every packet before opening the socket is what makes this
    # all-or-nothing: a typo in the second address must not leave the first
    # already sent and the command reporting failure.
    with pytest.raises(backends.BackendError):
        lgtv._send_magic_packets(
            ["4C:BC:E9:D7:F6:2A", "nonsense"],
            lgtv.DEFAULT_WOL_PORT,
            lgtv.DEFAULT_WOL_BROADCAST,
        )


def test_a_mac_is_recognised_however_it_is_spelled():
    assert lgtv._normalise_mac("4c-bc-e9-d7-f6-2a") == "4C:BC:E9:D7:F6:2A"
    assert lgtv._normalise_mac("4C:BC:E9:D7:F6:2A") == "4C:BC:E9:D7:F6:2A"
    assert lgtv._normalise_mac("4cbce9d7f62a") == "4C:BC:E9:D7:F6:2A"
    assert lgtv._normalise_mac("not-a-mac") == ""
    assert lgtv._normalise_mac("") == ""


async def test_the_macs_are_learned_from_the_tv_on_connection(fake):
    fake.connection_info = REAL_CONNECTION_INFO
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()

    assert device._macs() == ["74:E6:B8:6A:AF:E0", "4C:BC:E9:D7:F6:2A"]
    await device.close()


async def test_a_single_mac_stored_by_an_older_version_still_works(fake, packets):
    """An install that paired before this was a list keeps waking until the
    next connection replaces it, rather than losing power_on entirely."""
    device = make()
    device._save_key_file(client_key="known-key", mac="74:E6:B8:6A:AF:E0")

    assert device._macs() == ["74:E6:B8:6A:AF:E0"]

    await device.send("power_on", {})
    assert packets[0][0] == ["74:E6:B8:6A:AF:E0"]


async def test_a_stale_stored_mac_is_replaced_on_the_next_connection(fake):
    """The other half of that migration, and the thing that unsticks an
    install already holding the wrong single address: detection re-runs on
    every connection, not only when nothing is stored."""
    device = make()
    device._save_key_file(client_key="known-key", mac="74:E6:B8:6A:AF:E0")
    fake.connection_info = REAL_CONNECTION_INFO

    await device.connect()

    assert device._macs() == ["74:E6:B8:6A:AF:E0", "4C:BC:E9:D7:F6:2A"]
    await device.close()


async def test_re_cabling_the_tv_is_picked_up_rather_than_cached_forever(fake):
    device = make()
    device._save_key_file(client_key="known-key")
    fake.connection_info = {"wifiInfo": {"macAddress": "11:22:33:44:55:66"}}
    await device.connect()
    assert device._macs() == ["11:22:33:44:55:66"]
    await device.close()

    fake.connection_info = REAL_CONNECTION_INFO
    await device.connect()

    assert device._macs() == ["74:E6:B8:6A:AF:E0", "4C:BC:E9:D7:F6:2A"]
    await device.close()


async def test_a_manually_set_mac_is_never_overwritten_by_auto_detection(fake):
    fake.connection_info = REAL_CONNECTION_INFO
    device = make(config={"mac": "aa:aa:aa:aa:aa:aa"})
    device._save_key_file(client_key="known-key")
    await device.connect()

    assert device._macs() == ["AA:AA:AA:AA:AA:AA"]
    await device.close()


async def test_several_macs_can_be_given_by_hand(fake, packets):
    device = make(config={"mac": "aa:aa:aa:aa:aa:aa, bb-bb-bb-bb-bb-bb"})

    await device.send("power_on", {})

    assert packets[0][0] == ["AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"]


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------


async def test_pairing_succeeds_on_the_first_attempt(fake):
    device = make()
    hint = await device.pair_start()
    assert "TV" in hint

    await device.pair_finish("")  # nothing to type back -- see pair_input_label

    assert device._state == "connected"
    assert device._load_key_file()["client_key"] == "new-client-key"


async def test_a_declined_prompt_is_reported_and_stops_the_loop(fake):
    fake.fail_with = lgtv.WebOsTvPairError("declined")
    device = make()
    await device.pair_start()

    with pytest.raises(backends.BackendError, match="declined"):
        await device.pair_finish("")

    assert device._state == "unpaired"


async def test_pairing_keeps_reshowing_the_prompt_until_one_attempt_lands(fake, monkeypatch):
    """aiowebostv gives one registration attempt a shorter window than a
    person actually needs to walk to the TV and find its remote -- this
    retry loop is what closes that gap. Simulates two attempts missing the
    window (as a real one would time out) before a third succeeds."""
    attempts = {"n": 0}
    real_connect = FakeWebOsClient.connect

    async def flaky_connect(self):
        # Only registration attempts (client_key is None) are subject to the
        # missed-window simulation -- an attempt with an already-accepted
        # key, like the ordinary `connect()` pair_finish makes afterwards,
        # would not be waiting on a human and would not time out for real.
        if self.client_key is None:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TimeoutError("missed the window")
        return await real_connect(self)

    monkeypatch.setattr(FakeWebOsClient, "connect", flaky_connect)

    device = make()
    await device.pair_start()
    await device.pair_finish("")

    assert attempts["n"] == 3
    assert device._state == "connected"


async def test_pairing_gives_up_after_its_own_retry_window(fake, monkeypatch):
    monkeypatch.setattr(lgtv, "PAIR_RETRY_WINDOW", 0.0)
    fake.fail_with = OSError("no route to host")

    device = make()
    await device.pair_start()

    with pytest.raises(backends.BackendError, match="no response from the TV"):
        await device.pair_finish("")


async def test_pair_finish_without_pair_start_is_rejected(fake):
    device = make()
    with pytest.raises(backends.BackendError, match="not started"):
        await device.pair_finish("")


async def test_a_second_pair_start_cancels_the_first_attempt(fake, monkeypatch):
    monkeypatch.setattr(lgtv, "PAIR_RETRY_WINDOW", 5.0)
    fake.fail_with = OSError("still offline")

    device = make()
    await device.pair_start()
    first_task = device._pair_task

    fake.fail_with = None
    await device.pair_start()
    second_task = device._pair_task

    assert second_task is not first_task
    await device.pair_finish("")

    assert device._state == "connected"
    # The superseded attempt was actually cancelled, not just abandoned --
    # otherwise it would keep re-registering (and re-prompting the TV) in
    # the background forever.
    assert first_task.cancelled()


# --------------------------------------------------------------------------
# Sending commands
# --------------------------------------------------------------------------


async def test_a_named_button_sends_the_matching_webos_button_name(fake):
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()

    await device.send("dpad_up", {})
    await device.send("mute", {})

    assert ("button", "UP") in device._client.sent
    assert ("button", "MUTE") in device._client.sent

    await device.close()


async def test_the_raw_button_command_needs_a_name():
    device = make()
    with pytest.raises(backends.BackendError, match="needs a name"):
        await device.send("button", {})


async def test_set_volume_validates_its_range(fake):
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()

    with pytest.raises(backends.BackendError, match="between 0 and 100"):
        await device.send("set_volume", {"level": 101})

    await device.send("set_volume", {"level": 40})
    assert ("set_volume", 40) in device._client.sent

    await device.close()


async def test_an_unknown_command_is_reported_rather_than_ignored(fake):
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()

    with pytest.raises(backends.BackendError, match="no command"):
        await device.send("does_not_exist", {})

    await device.close()


# --------------------------------------------------------------------------
# Inputs and apps
# --------------------------------------------------------------------------


async def test_entities_lists_inputs_and_apps_for_the_picker(fake):
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()
    device._client.inputs = [{"id": "HDMI_1", "label": "HDMI 1", "connected": True}]
    device._client.apps = [{"id": "netflix", "title": "Netflix"}]

    found = await device.entities()

    assert {
        "entity_id": "input:HDMI_1",
        "name": "HDMI 1",
        "domain": "input",
        "state": "connected",
        "controllable": True,
    } in found
    assert {
        "entity_id": "app:netflix",
        "name": "Netflix",
        "domain": "app",
        "state": "",
        "controllable": True,
    } in found

    await device.close()


async def test_a_chosen_input_becomes_a_named_command_and_can_be_sent(fake):
    device = make(config={"entities": ["input:HDMI_1"]})
    device._save_key_file(client_key="known-key")
    await device.connect()

    names = {c.name for c in await device.commands()}
    assert "input:HDMI_1" in names

    await device.send("input:HDMI_1", {})
    assert ("set_input", "HDMI_1") in device._client.sent

    await device.close()


async def test_an_input_nobody_picked_is_not_a_valid_command(fake):
    # The same protection Home Assistant's entity commands get: a typo in a
    # raw id fails at press time, but sending to something never picked from
    # the list should fail just as loudly rather than quietly reaching it.
    device = make()
    device._save_key_file(client_key="known-key")
    await device.connect()

    with pytest.raises(backends.BackendError, match="no command"):
        await device.send("input:HDMI_1", {})

    await device.close()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


async def test_a_tv_is_found_and_named_by_its_friendly_name(monkeypatch):
    async def fake_search(timeout, targets):
        assert targets == lgtv._SEARCH_TARGETS
        return ["http://10.0.0.9:1400/description.xml"]

    monkeypatch.setattr(_ssdp, "search", fake_search)

    class FakeResponse:
        text = "<manufacturer>LG Electronics</manufacturer><friendlyName>Lounge TV</friendlyName>"

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(lgtv.httpx, "AsyncClient", lambda **kw: FakeHttpClient())

    found = await lgtv.discover()

    assert found == [{"name": "Lounge TV", "host": "10.0.0.9", "model": ""}]


async def test_nothing_answering_ssdp_is_an_empty_list_not_an_error(monkeypatch):
    async def raises(timeout, targets):
        raise OSError("no usable network interface")

    monkeypatch.setattr(_ssdp, "search", raises)

    assert await lgtv.discover() == []
