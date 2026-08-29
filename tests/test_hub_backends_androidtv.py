"""The Android TV backend, driven against a stand-in for the real client.

Nothing here talks to a television. What is worth testing without one is the
behaviour that is easy to get wrong and expensive to discover in the living
room: that an unreachable device still starts, that POWER is not sent to a
device already in the state being asked for, and that the two vocabularies --
the remote's button keys and the TV's commands -- have not drifted apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harmony_hub import backends
from harmony_hub.backends import androidtv
from harmony_hub.backends.androidtv import AndroidTvBackend

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeRemote:
    """Stands in for `AndroidTVRemote`, recording what it was asked to send."""

    #: What the next `async_connect` should do: None to succeed, or an
    #: exception instance to raise.
    fail_with: Exception | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.host = kwargs.get("host")
        self.sent: list[tuple] = []
        self.launched: list[str] = []
        self.typed: list[str] = []
        self.is_on = None
        self.current_app = None
        self.connected = False
        self.reconnecting = False
        self.reconnect_calls = 0
        self.availability_callbacks = []
        self.paired_with: str | None = None
        self.pairing_started = False

    async def async_generate_cert_if_missing(self) -> bool:
        for path in (self.kwargs["certfile"], self.kwargs["keyfile"]):
            Path(path).write_text("not really a certificate", encoding="utf-8")
        return True

    async def async_connect(self) -> None:
        if type(self).fail_with is not None:
            raise type(self).fail_with
        self.connected = True
        self.is_on = True

    def keep_reconnecting(self, invalid_auth_callback=None) -> None:
        self.reconnecting = True
        self.reconnect_calls += 1

    def add_is_available_updated_callback(self, callback) -> None:
        self.availability_callbacks.append(callback)

    def disconnect(self) -> None:
        self.connected = False
        self.is_on = None

    def send_key_command(self, key_code, direction="SHORT") -> None:
        if not self.connected:
            raise RuntimeError("Called send_key_command after disconnect")
        self.sent.append((key_code, direction))

    def send_launch_app_command(self, app: str) -> None:
        self.launched.append(app)

    def send_text(self, text: str) -> None:
        self.typed.append(text)

    async def async_start_pairing(self) -> None:
        self.pairing_started = True

    async def async_finish_pairing(self, code: str) -> None:
        self.paired_with = code
        self.disconnect()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Substitutes the client, and keeps certificates inside tmp_path."""
    FakeRemote.fail_with = None
    monkeypatch.setattr(androidtv, "AndroidTVRemote", FakeRemote)
    monkeypatch.chdir(tmp_path)
    yield FakeRemote
    FakeRemote.fail_with = None


def make(config=None) -> AndroidTvBackend:
    return AndroidTvBackend("shield", {"host": "10.0.0.5", **(config or {})})


# --------------------------------------------------------------------------
# Registration and the command table
# --------------------------------------------------------------------------


def test_the_backend_is_registered_and_pairable():
    assert backends.get("androidtv") is AndroidTvBackend
    assert issubclass(AndroidTvBackend, backends.Pairable)


def test_the_device_form_asks_for_an_address():
    schema = AndroidTvBackend.config_schema()

    assert schema["required"] == ["host"]
    assert schema["properties"]["host"]["title"] == "Address"
    # A flat schema is what makes the generated form render real fields
    # instead of a JSON blob, so no property may be an object or an array.
    assert {p["type"] for p in schema["properties"].values()} <= {"string", "integer", "boolean"}


async def test_commands_cover_the_remote_and_mark_what_may_repeat():
    commands = await make().commands()
    names = {c.name for c in commands}

    assert {"dpad_up", "select", "back", "home", "volume_up", "play", "digit_0"} <= names
    assert {"power_on", "power_off", "launch_app", "text", "key"} <= names

    repeatable = {c.name for c in commands if c.repeatable}
    assert {"volume_up", "dpad_down", "channel_up"} <= repeatable
    # Repeating these while a button is held would be actively wrong.
    assert not repeatable & {"power", "power_on", "power_off", "select", "play"}


def test_every_key_code_is_one_the_protocol_knows():
    """A typo here would fail at press time, on a real remote, in the dark."""
    from androidtvremote2.remotemessage_pb2 import RemoteKeyCode

    known = set(RemoteKeyCode.keys())
    unknown = [name for name, code in androidtv.KEY_CODES.items() if f"KEYCODE_{code}" not in known]
    assert unknown == []


def test_the_suggested_mapping_joins_two_real_vocabularies():
    """Keeps `buttons.json` and the command table from drifting apart."""
    buttons = set(json.loads((REPO_ROOT / "buttons.json").read_text(encoding="utf-8")))
    commands = {c.name for c in androidtv.COMMANDS}

    assert set(androidtv.SUGGESTED_BINDINGS) <= buttons
    assert set(androidtv.SUGGESTED_BINDINGS.values()) <= commands
    # The activity and SmartHome keys belong to scenes and to lighting.
    assert not set(androidtv.SUGGESTED_BINDINGS) & {"consumer_0x01e8", "consumer_0x0ff0"}
    assert make().suggested_bindings() == androidtv.SUGGESTED_BINDINGS


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


async def test_connecting_reaches_the_device_and_reports_what_it_is_doing(fake):
    device = make()
    await device.connect()

    health = await device.health()
    assert health.ok
    assert health.detail == "on"
    assert device._remote.reconnecting, "the library should own reconnection once connected"

    await device.close()


async def test_an_unpaired_device_still_starts_so_that_it_can_be_paired(fake):
    """The engine drops a backend whose connect() raises -- and a dropped
    backend cannot be reached by the pairing routes, which would leave an
    unpaired device permanently unpairable."""
    fake.fail_with = androidtv.InvalidAuth("Need to pair")

    device = make()
    await device.connect()  # must not raise

    health = await device.health()
    assert not health.ok
    assert "not paired" in health.detail
    assert device._retry is None, "retrying cannot fix a missing pairing"

    await device.close()


async def test_an_unreachable_device_starts_and_keeps_trying(fake):
    fake.fail_with = androidtv.CannotConnect("no route")

    device = make()
    await device.connect()

    health = await device.health()
    assert not health.ok
    assert "cannot reach 10.0.0.5" in health.detail
    assert device._retry is not None and not device._retry.done()

    await device.close()
    assert device._retry is None


async def test_a_device_with_no_address_says_so_instead_of_failing(fake):
    device = AndroidTvBackend("shield", {})
    await device.connect()

    assert (await device.health()).detail == "no address set"


async def test_closing_disconnects_and_forgets_the_client(fake):
    device = make()
    await device.connect()
    remote = device._remote

    await device.close()

    assert not remote.connected
    assert device._remote is None


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


async def test_a_named_command_becomes_its_android_key_code(fake):
    device = make()
    await device.connect()

    await device.send("dpad_up")
    await device.send("select")
    await device.send("digit_7")

    assert device._remote.sent == [
        ("DPAD_UP", "SHORT"),
        ("DPAD_CENTER", "SHORT"),
        ("7", "SHORT"),
    ]
    await device.close()


async def test_raw_key_codes_and_long_presses_go_through_params(fake):
    device = make()
    await device.connect()

    await device.send("key", {"key_code": "BOOKMARK", "direction": "START_LONG"})

    assert device._remote.sent == [("BOOKMARK", "START_LONG")]
    await device.close()


async def test_a_named_command_can_ask_for_a_direction_too(fake):
    """A button bound to 'Select' should be able to hold, not just tap.

    This is what makes a press/release pair on the remote produce a real
    Android long press -- e.g. YouTube's context menu on OK -- without the
    binding having to fall back to the raw 'key' command.
    """
    device = make()
    await device.connect()

    await device.send("select", {"direction": "START_LONG"})
    await device.send("select", {"direction": "END_LONG"})

    assert device._remote.sent == [("DPAD_CENTER", "START_LONG"), ("DPAD_CENTER", "END_LONG")]
    assert device._held == {}
    await device.close()


async def test_an_unknown_direction_is_rejected(fake):
    device = make()
    await device.connect()

    with pytest.raises(backends.BackendError, match="direction"):
        await device.send("select", {"direction": "SIDEWAYS"})
    await device.close()


async def test_a_second_start_long_on_the_same_key_closes_the_first(fake):
    """A lost release before a fresh press must not orphan the earlier hold."""
    device = make()
    await device.connect()

    await device.send("key", {"key_code": "DPAD_CENTER", "direction": "START_LONG"})
    await device.send("key", {"key_code": "DPAD_CENTER", "direction": "START_LONG"})

    assert device._remote.sent == [
        ("DPAD_CENTER", "START_LONG"),
        ("DPAD_CENTER", "END_LONG"),
        ("DPAD_CENTER", "START_LONG"),
    ]
    assert list(device._held) == ["DPAD_CENTER"]
    await device.close()


async def test_a_key_held_past_the_limit_is_released_by_the_watchdog(fake, monkeypatch):
    """A dropped release must not leave a key down on the TV forever."""
    monkeypatch.setattr(androidtv, "MAX_HOLD_SECONDS", 0.05)
    device = make()
    await device.connect()

    await device.send("key", {"key_code": "DPAD_CENTER", "direction": "START_LONG"})
    assert device._held  # the watchdog is armed
    await device._held["DPAD_CENTER"]

    assert device._remote.sent == [
        ("DPAD_CENTER", "START_LONG"),
        ("DPAD_CENTER", "END_LONG"),
    ]
    assert device._held == {}
    await device.close()


async def test_closing_while_a_key_is_held_does_not_crash(fake):
    device = make()
    await device.connect()

    await device.send("key", {"key_code": "DPAD_CENTER", "direction": "START_LONG"})
    await device.close()

    assert device._held == {}


async def test_the_hold_command_sends_an_atomic_long_press(fake):
    device = make()
    await device.connect()

    await device.send("hold", {"key_code": "DPAD_CENTER", "hold_secs": 0.01})

    assert device._remote.sent == [
        ("DPAD_CENTER", "START_LONG"),
        ("DPAD_CENTER", "END_LONG"),
    ]
    assert device._held == {}
    await device.close()


async def test_the_hold_command_defaults_to_select(fake):
    device = make()
    await device.connect()

    await device.send("hold", {"hold_secs": 0.01})

    assert device._remote.sent == [
        ("DPAD_CENTER", "START_LONG"),
        ("DPAD_CENTER", "END_LONG"),
    ]
    await device.close()


async def test_launching_an_app_and_typing_text(fake):
    device = make()
    await device.connect()

    await device.send("launch_app", {"app": "https://www.netflix.com"})
    await device.send("text", {"text": "the wire"})

    assert device._remote.launched == ["https://www.netflix.com"]
    assert device._remote.typed == ["the wire"]
    await device.close()


async def test_a_parameterised_command_missing_its_parameter_is_an_error(fake):
    device = make()
    await device.connect()

    with pytest.raises(backends.BackendError, match="key_code"):
        await device.send("key", {})

    await device.close()


async def test_an_unknown_command_is_an_error_rather_than_a_silent_no_op(fake):
    device = make()
    await device.connect()

    with pytest.raises(backends.BackendError, match="no command 'teleport'"):
        await device.send("teleport")

    await device.close()


async def test_sending_to_a_device_that_never_connected_explains_why(fake):
    fake.fail_with = androidtv.CannotConnect("no route")
    device = make()
    await device.connect()

    with pytest.raises(backends.BackendError, match="unreachable"):
        await device.send("home")

    await device.close()


async def test_a_dropped_socket_is_reconnected_on_the_next_command(fake):
    device = make()
    await device.connect()
    device._remote.disconnect()  # as if the TV had gone away and come back

    await device.send("home")

    assert device._remote.sent == [("HOME", "SHORT")]
    await device.close()


async def test_reconnecting_does_not_pile_up_watchers(fake):
    """`keep_reconnecting` starts a task per call without stopping the last
    one, and the callback list only grows -- so both are set up once per
    client, not once per connection attempt."""
    device = make()
    await device.connect()

    for _ in range(3):
        device._remote.disconnect()
        await device.send("home")

    assert device._remote.reconnect_calls == 1
    assert len(device._remote.availability_callbacks) == 1
    await device.close()


# --------------------------------------------------------------------------
# Power
# --------------------------------------------------------------------------


async def test_power_on_does_nothing_when_the_device_is_already_on(fake):
    """POWER is a toggle: sending it to an on device would switch it off,
    which is exactly what a scene switch must not do."""
    device = make()
    await device.connect()
    device._remote.is_on = True

    await device.send("power_on")

    assert device._remote.sent == []
    await device.close()


async def test_power_on_sends_power_when_the_device_is_off(fake):
    device = make()
    await device.connect()
    device._remote.is_on = False

    await device.send("power_on")
    assert device._remote.sent == [("POWER", "SHORT")]

    # And now that it is on, asking again changes nothing.
    device._remote.is_on = True
    await device.send("power_on")
    assert device._remote.sent == [("POWER", "SHORT")]

    await device.close()


async def test_power_off_sends_power_only_when_the_device_is_on(fake):
    device = make()
    await device.connect()

    device._remote.is_on = True
    await device.send("power_off")
    device._remote.is_on = False
    await device.send("power_off")

    assert device._remote.sent == [("POWER", "SHORT")]
    await device.close()


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------


async def test_pairing_asks_for_a_code_and_then_reconnects(fake):
    fake.fail_with = androidtv.InvalidAuth("Need to pair")
    device = make()
    await device.connect()

    detail = await device.pair_start()
    assert "code" in detail.lower()
    assert device._remote.pairing_started

    # The device accepts us from here on.
    fake.fail_with = None
    await device.pair_finish(" 123456 ")

    assert (await device.health()).ok
    await device.close()


async def test_a_rejected_code_is_reported_and_leaves_the_device_unpaired(fake):
    class Rejecting(FakeRemote):
        async def async_finish_pairing(self, code):
            raise androidtv.InvalidAuth("bad code")

    fake.fail_with = androidtv.InvalidAuth("Need to pair")
    device = make()
    await device.connect()
    device._remote.__class__ = Rejecting

    with pytest.raises(backends.BackendError, match="not accepted"):
        await device.pair_finish("000000")

    assert not (await device.health()).ok
    await device.close()


async def test_certificates_are_kept_out_of_the_configuration(fake, tmp_path):
    """A paired certificate is secret and machine-specific: it belongs on
    disk, not in a config file that round-trips through the device form."""
    device = make()
    await device.connect()

    certfile = tmp_path / "credentials" / "androidtv_shield.crt"
    assert certfile.is_file()
    assert device.config == {"host": "10.0.0.5"}

    await device.close()


async def test_the_certificate_directory_can_be_moved(fake, tmp_path):
    device = make({"cert_dir": str(tmp_path / "elsewhere")})
    await device.connect()

    assert (tmp_path / "elsewhere" / "androidtv_shield.key").is_file()
    await device.close()
