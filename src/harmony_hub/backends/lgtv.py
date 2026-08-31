"""LG webOS TVs, over LG's own SSAP (Simple Service Access Protocol).

Every webOS TV since 2014 -- including the OLED range -- runs a websocket
server on port 3000 (falling back to TLS on 3001 on newer firmware) that
speaks the same JSON control protocol the LG ThinQ phone app and the TV's
own Magic Remote use. No developer mode, no ADB: the one thing it needs is a
one-time "allow this device?" prompt accepted on the TV's own remote, the
same shape as the other two pairable backends but for one important
difference explained below.

**Registration and the pairing prompt are the same network round trip.**
Android TV separates "prove who I am" (a certificate, checked instantly)
from "let a human approve me" (a code typed back once); webOS folds both
into one `connect()` call that blocks until either the client key it was
given is accepted immediately, or -- when given no key at all -- a human
presses Accept on the TV within the *library's own* short receive window
(a matter of seconds, not the tens of seconds a person actually needs to
walk over and find the remote). `pair_start()` therefore does not make one
attempt and wait; it keeps re-registering in the background, each attempt
re-showing the TV's prompt, until one of them lands inside a human's
reaction time. This is the one piece of this file not exercised against a
real set -- worth confirming a repeated registration refreshes the same
prompt on-screen rather than stacking up several.

**Power on is not in the API at all.** `system/turnOn` only works once a
websocket is already open, which by definition it is not on a TV that is
fully off. The universal answer -- the same one Home Assistant's own
integration relies on -- is Wake-on-LAN: a magic packet to the TV's MAC,
which needs "Mobile TV On" (or "Turn on via Wi-Fi") enabled in the TV's own
settings. So `power_on` here sends a magic packet rather than an SSAP call,
and works whether or not a websocket connection currently exists. The MAC
itself does not have to be typed in: once paired, the TV reports its
interfaces over `connectionmanager/getinfo`, so every connection caches them
next to the client key and the config field stays there only as an override.

The catch, and it is the whole reason `_refresh_macs` is written the way it
is: the TV reports a MAC for *every* interface it has, whether or not that
interface is plugged in or associated, and gives no field saying which one
is carrying traffic. So there is no "the" MAC to pick. `power_on` wakes both
the wired and the wi-fi one; a magic packet to an interface that is not
listening does nothing at all, which makes the spare free.

**Inputs and apps are chosen, the same way Home Assistant's entities are.**
A webOS TV has a handful of HDMI inputs and dozens of installed apps; typing
either as a raw string is exactly the failure `Command` exists to prevent,
so both come from `config["entities"]` and the same "Choose entities" picker,
generating one named command per input or app actually picked rather than a
free-text `set_input`/`launch_app` for everything else to fall back on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from . import Backend, BackendError, Command, Health, Pairable, Readable, StateTarget, register
from . import _ssdp

logger = logging.getLogger("HUB.lgtv")

try:
    from aiowebostv import WebOsClient
    from aiowebostv.exceptions import WebOsTvPairError

    IMPORT_ERROR = ""
except ImportError as err:  # pragma: no cover - only without the dependency
    WebOsClient = None
    # A placeholder so the `except` clauses below stay valid. Nothing reaches
    # it: every path checks `WebOsClient is None` first.
    WebOsTvPairError = OSError
    IMPORT_ERROR = str(err)

#: Where the paired client key (and the auto-detected MAC next to it) is
#: kept, relative to the working directory -- the same convention
#: `hub_config.json` and the Android TV certificates already follow.
DEFAULT_CREDENTIALS_DIR = "credentials"

#: SSDP search target webOS TVs answer to.
_SEARCH_TARGETS = ("urn:lge-com:service:webos-second-screen:1",)

#: Matched case-insensitively as a substring of the description's
#: `<manufacturer>`. Only checked against one real description.xml's worth
#: of prior art -- if a TV goes unfound, this is the first thing to widen.
_MANUFACTURERS = ("lg electronics",)

#: Long enough for the handshake -- hello, pre-registration system info, an
#: already-accepted registration, the input socket, and eight state
#: subscriptions -- over a slow wifi link.
CONNECT_TIMEOUT = 15.0

RETRY_MIN_SECONDS = 2.0
RETRY_MAX_SECONDS = 60.0

#: How long one registration attempt is given to receive a response before
#: this backend gives up on it and tries again. A little above the
#: library's own internal receive timeout for the pairing prompt.
PAIR_ATTEMPT_TIMEOUT = 12.0
#: Total time `pair_start()` keeps re-showing the prompt for. Long enough
#: for someone to read the hint, walk to the TV, and find its remote.
PAIR_RETRY_WINDOW = 90.0
PAIR_RETRY_GAP = 1.0

#: Default port a webOS TV's Wake-on-LAN listener answers on.
DEFAULT_WOL_PORT = 9
DEFAULT_WOL_BROADCAST = "255.255.255.255"


def _why(err: BaseException) -> str:
    """A readable reason for an exception that may carry no message at all."""
    return str(err) or type(err).__name__


def _int_param(params: Dict[str, Any], key: str, low: int, high: int, command: str) -> int:
    try:
        value = int(params.get(key))
    except (TypeError, ValueError):
        raise BackendError(f"'{command}' needs an integer '{key}'") from None
    if not (low <= value <= high):
        raise BackendError(f"'{command}' needs a '{key}' between {low} and {high}, got {value}")
    return value


def _normalise_mac(mac: str) -> str:
    """A MAC as uppercase colon-separated, or "" if it is not one.

    Used both to validate and to compare: the TV reports `4C:BC:E9:...`
    while somebody typing one into the device form is as likely to use
    dashes or lower case, and two spellings of the same address must not
    look like two different interfaces.
    """
    digits = mac.replace(":", "").replace("-", "").replace(".", "").strip()
    try:
        raw = bytes.fromhex(digits)
    except ValueError:
        return ""
    if len(raw) != 6:
        return ""
    return ":".join(f"{b:02X}" for b in raw)


def _send_magic_packets(macs: List[str], port: int, broadcast: str) -> None:
    """One Wake-on-LAN magic packet per MAC: 6 bytes of 0xFF, then the MAC 16 times.

    Every MAC the TV reports gets one, rather than this trying to work out
    which interface is live -- see `_refresh_macs` for why that guess cannot
    be made from what the TV tells us. A packet addressed to an interface
    that is not listening is inert, so the spare costs nothing.
    """
    packets = []
    for mac in macs:
        normalised = _normalise_mac(mac)
        if not normalised:
            raise BackendError(f"'{mac}' does not look like a MAC address")
        packets.append(b"\xff" * 6 + bytes.fromhex(normalised.replace(":", "")) * 16)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for packet in packets:
            sock.sendto(packet, (broadcast, port))
    finally:
        sock.close()


# --------------------------------------------------------------------------
# Command table
# --------------------------------------------------------------------------

# name, webOS button name, label, safe to repeat while the button is held
_BUTTONS: Tuple[Tuple[str, str, str, bool], ...] = (
    ("dpad_up", "UP", "Up", True),
    ("dpad_down", "DOWN", "Down", True),
    ("dpad_left", "LEFT", "Left", True),
    ("dpad_right", "RIGHT", "Right", True),
    ("select", "ENTER", "Select", False),
    ("back", "BACK", "Back", False),
    ("home", "HOME", "Home", False),
    ("exit", "EXIT", "Exit", False),
    ("menu", "MENU", "Menu", False),
    ("info", "INFO", "Info", False),
    ("guide", "GUIDE", "Guide", False),
    ("app_list", "LIST", "Apps list", False),
    ("mute", "MUTE", "Mute", False),
    ("volume_up", "VOLUMEUP", "Volume up", True),
    ("volume_down", "VOLUMEDOWN", "Volume down", True),
    ("channel_up", "CHANNELUP", "Channel up", True),
    ("channel_down", "CHANNELDOWN", "Channel down", True),
    ("play", "PLAY", "Play", False),
    ("pause", "PAUSE", "Pause", False),
    ("stop", "STOP", "Stop", False),
    ("rewind", "REWIND", "Rewind", True),
    ("fast_forward", "FASTFORWARD", "Fast forward", True),
    ("captions", "CC", "Subtitles", False),
    ("red", "RED", "Red", False),
    ("green", "GREEN", "Green", False),
    ("yellow", "YELLOW", "Yellow", False),
    ("blue", "BLUE", "Blue", False),
    ("digit_0", "0", "0", False),
    ("digit_1", "1", "1", False),
    ("digit_2", "2", "2", False),
    ("digit_3", "3", "3", False),
    ("digit_4", "4", "4", False),
    ("digit_5", "5", "5", False),
    ("digit_6", "6", "6", False),
    ("digit_7", "7", "7", False),
    ("digit_8", "8", "8", False),
    ("digit_9", "9", "9", False),
)

BUTTON_NAMES: Dict[str, str] = {name: webos for name, webos, _label, _repeat in _BUTTONS}

#: Buttons from `buttons.json` mapped to the command that suits them.
SUGGESTED_BINDINGS: Dict[str, str] = {
    "up_arrow": "dpad_up",
    "down_arrow": "dpad_down",
    "left_arrow": "dpad_left",
    "right_arrow": "dpad_right",
    "keypad_enter": "select",
    "enter": "select",
    "ac_back": "back",
    "quit": "back",
    "media_select_home": "home",
    "application_menu_key": "menu",
    "program_guide": "guide",
    "consumer_0x01ff": "info",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "mute": "mute",
    "channel_up": "channel_up",
    "channel_down": "channel_down",
    "play": "play",
    "pause": "pause",
    "stop": "stop",
    "fast_forward": "fast_forward",
    "rewind": "rewind",
    "colour_red": "red",
    "colour_green": "green",
    "colour_yellow": "yellow",
    "colour_blue": "blue",
    "0": "digit_0",
    "1": "digit_1",
    "2": "digit_2",
    "3": "digit_3",
    "4": "digit_4",
    "5": "digit_5",
    "6": "digit_6",
    "7": "digit_7",
    "8": "digit_8",
    "9": "digit_9",
}


def _build_commands() -> List[Command]:
    commands = [
        Command(name=name, label=label, description=f"Remote button {webos}.", repeatable=repeatable)
        for name, webos, label, repeatable in _BUTTONS
    ]
    commands += [
        Command(
            name="power_on",
            label="Power on",
            description="Sends a Wake-on-LAN magic packet. Needs 'Mobile TV On' (or "
            "'Turn on via Wi-Fi') enabled on the TV, and a MAC address on file -- "
            "connect once with the TV already on and it is picked up automatically.",
        ),
        Command(
            name="power_off",
            label="Power off",
            description="Standby. Only sent if the TV is currently on.",
        ),
        Command(
            name="screen_on",
            label="Screen on",
            description="Turns the panel back on after 'Screen off'.",
        ),
        Command(
            name="screen_off",
            label="Screen off",
            description="Turns the panel off while audio keeps playing -- for music "
            "without the picture.",
        ),
        Command(
            name="set_volume",
            label="Set volume",
            params={
                "type": "object",
                "required": ["level"],
                "properties": {
                    "level": {"type": "integer", "title": "Level", "minimum": 0, "maximum": 100}
                },
            },
        ),
        Command(
            name="set_channel",
            label="Set channel",
            params={
                "type": "object",
                "required": ["channel"],
                "properties": {"channel": {"type": "string", "title": "Channel"}},
            },
        ),
        Command(
            name="set_input",
            label="Switch input (raw)",
            description="A raw input id, e.g. HDMI_1. Prefer a picked input below, "
            "if one is listed -- it reads better on the binding screen.",
            params={
                "type": "object",
                "required": ["input"],
                "properties": {"input": {"type": "string", "title": "Input id"}},
            },
        ),
        Command(
            name="launch_app",
            label="Launch app (raw)",
            description="A raw app id, e.g. netflix or youtube.leanback.v4. Prefer a "
            "picked app below, if one is listed.",
            params={
                "type": "object",
                "required": ["app"],
                "properties": {"app": {"type": "string", "title": "App id"}},
            },
        ),
        Command(
            name="sound_output",
            label="Sound output",
            description="e.g. tv_speaker, external_arc, external_optical, "
            "external_speaker, bt_soundbar -- which of these exist depends on the "
            "model.",
            params={
                "type": "object",
                "required": ["output"],
                "properties": {"output": {"type": "string", "title": "Output"}},
            },
        ),
        Command(
            name="toast",
            label="Show message",
            description="A floating on-screen notification -- handy for diagnostics.",
            params={
                "type": "object",
                "required": ["message"],
                "properties": {"message": {"type": "string", "title": "Message"}},
            },
        ),
        Command(
            name="button",
            label="Raw button",
            description="Any webOS remote button name this list does not have.",
            params={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string", "title": "Button name"}},
            },
        ),
    ]
    return commands


COMMANDS: List[Command] = _build_commands()


# --------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------


@register
class LgTvBackend(Backend, Pairable, Readable):
    """One LG webOS TV -- OLED, QNED, or otherwise -- over SSAP."""

    name = "lgtv"
    label = "LG webOS TV"
    description = (
        "An LG TV running webOS, over the same control protocol the LG ThinQ app and "
        "Magic Remote use. Pairing is a prompt accepted on the TV, no code to type."
    )
    discover_field = "host"

    pair_label = "Pair this TV"
    pair_hint = (
        "A connection prompt will appear on the TV -- accept it with the TV's own "
        "remote, not this app. If you miss it, it keeps reappearing for a while."
    )
    pair_input_label = ""  # nothing to type back; see pair_finish
    pair_input_multiline = False

    def __init__(self, device_id: str, config: Dict[str, Any]) -> None:
        super().__init__(device_id, config)
        self._client: Optional[Any] = None
        self._retry: Optional[asyncio.Task] = None
        self._watching = False
        self._state = "stopped"
        self._detail = "not started"
        self._names: Dict[str, str] = {}
        #: The in-progress registration retry loop started by `pair_start()`.
        self._pair_task: Optional[asyncio.Task] = None
        #: The client it eventually succeeds with, kept so `pair_finish()`
        #: can read `client_key` off it and hand it to the real `connect()`.
        self._pair_client: Optional[Any] = None

    # -- configuration ----------------------------------------------------

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["host"],
            "properties": {
                "host": {
                    "type": "string",
                    "title": "Address",
                    "description": "IP address or hostname. Give it a fixed address in "
                    "the router -- the connection drops when it moves.",
                    "default": "",
                },
                "mac": {
                    "type": "string",
                    "title": "MAC address",
                    "description": "For Wake-on-LAN. Leave blank: the TV's own addresses "
                    "are picked up automatically on every connection, and both its wired "
                    "and wi-fi interfaces are woken, so this only needs setting by hand if "
                    "that lookup fails. Several may be given, separated by commas.",
                    "default": "",
                },
                "wol_port": {
                    "type": "integer",
                    "title": "Wake-on-LAN port",
                    "default": DEFAULT_WOL_PORT,
                },
                "wol_broadcast": {
                    "type": "string",
                    "title": "Wake-on-LAN broadcast address",
                    "description": "Usually fine left at the default unless the TV is on "
                    "a different subnet.",
                    "default": DEFAULT_WOL_BROADCAST,
                },
                "key_dir": {
                    "type": "string",
                    "title": "Key directory",
                    "description": "Where the paired client key is kept. Blank means "
                    f"'{DEFAULT_CREDENTIALS_DIR}'.",
                    "default": "",
                },
                "entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "title": "Inputs & apps",
                    "description": "Which inputs and apps this device offers as named "
                    "commands. Use 'Choose entities' rather than typing them.",
                    "default": [],
                },
            },
        }

    @property
    def host(self) -> str:
        return str(self.config.get("host") or "").strip()

    @property
    def exposed(self) -> List[str]:
        entities = self.config.get("entities") or []
        return [str(e).strip() for e in entities if str(e).strip()]

    def _key_path(self) -> Path:
        directory = Path(str(self.config.get("key_dir") or "").strip() or DEFAULT_CREDENTIALS_DIR)
        return directory / f"lgtv_{self.device_id}.json"

    def _load_key_file(self) -> Dict[str, str]:
        path = self._key_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_key_file(self, **updates: Any) -> None:
        path = self._key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load_key_file()
        data.update({k: v for k, v in updates.items() if v})
        path.write_text(json.dumps(data), encoding="utf-8")

    def _cached_macs(self) -> List[str]:
        """Every MAC learned from the TV, newest format first.

        `macs` is a list because a TV has more than one interface and only
        one of them is live; `mac` is the single string an older version of
        this backend wrote, read here so an existing install keeps working
        until the next connection replaces it.
        """
        data = self._load_key_file()
        stored = data.get("macs")
        if isinstance(stored, list):
            return [m for m in (_normalise_mac(str(e)) for e in stored) if m]
        legacy = _normalise_mac(str(data.get("mac") or ""))
        return [legacy] if legacy else []

    def _macs(self) -> List[str]:
        """What `power_on` should wake, manual override winning over what was learned."""
        manual = [
            m
            for m in (_normalise_mac(part) for part in str(self.config.get("mac") or "").split(","))
            if m
        ]
        return manual or self._cached_macs()

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Builds the client and tries once to reach the device.

        Never raises. Every failure becomes a state that `health()` explains
        and, where retrying could help, a background task that keeps trying.
        """
        await self.close()

        if WebOsClient is None:
            self._set_state("unavailable", f"aiowebostv is not installed ({IMPORT_ERROR})")
            return
        if not self.host:
            self._set_state("unconfigured", "no address set")
            return

        key = str(self._load_key_file().get("client_key") or "").strip()
        if not key:
            # Building a client with no key would send registration -- which
            # is also what puts the accept/deny prompt on the TV's screen.
            # Fine once, deliberately, from `pair_start()`; not on every hub
            # startup and retry, which would otherwise flash a prompt at
            # nobody every time the TV happened to be reachable.
            self._set_state("unpaired", "not paired -- pair this device to use it")
            return

        self._client = WebOsClient(self.host, client_key=key)
        await self._try_connect()

    async def close(self) -> None:
        if self._retry is not None:
            self._retry.cancel()
            self._retry = None
        if self._pair_task is not None:
            self._pair_task.cancel()
            self._pair_task = None
        if self._pair_client is not None:
            await self._pair_client.disconnect()
            self._pair_client = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
        self._watching = False
        self._set_state("stopped", "not started")

    def _set_state(self, state: str, detail: str) -> None:
        if (state, detail) != (self._state, self._detail):
            logger.info(
                "[lgtv:%s] %s%s", self.device_id, state, f" -- {detail}" if detail else ""
            )
        self._state = state
        self._detail = detail

    async def _try_connect(self) -> str:
        """One connection attempt on the existing client. Returns the state it left behind."""
        try:
            await asyncio.wait_for(self._client.connect(), timeout=CONNECT_TIMEOUT)
        except WebOsTvPairError as err:
            # A stale or revoked key: the TV no longer recognises it and
            # treats this the same as an unregistered client, which means it
            # is showing a prompt right now with nobody there to accept it.
            # Retrying cannot help -- only re-pairing can.
            self._set_state("unpaired", f"the TV rejected this pairing -- pair it again ({_why(err)})")
        except (OSError, TimeoutError, asyncio.TimeoutError) as err:
            self._set_state("unreachable", f"cannot reach {self.host} ({_why(err)})")
            self._schedule_retry()
        except Exception as err:  # pragma: no cover - defensive
            self._set_state("error", str(err))
        else:
            self._set_state("connected", "")
            if not self._watching:
                self._watching = True
                await self._client.register_state_update_callback(self._on_state_update)
            await self._refresh_macs()
            if self.exposed:
                await self._refresh_names()
        return self._state

    async def _on_state_update(self, state: Any) -> None:
        if not state.is_on and self._state == "connected":
            self._set_state("unreachable", f"lost the connection to {self.host}, retrying")
            self._schedule_retry()

    def _schedule_retry(self) -> None:
        if self._retry is not None and not self._retry.done():
            return
        self._retry = asyncio.create_task(self._retry_until_connected())

    async def _retry_until_connected(self) -> None:
        delay = RETRY_MIN_SECONDS
        while True:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self._client is None:
                return
            if await self._try_connect() != "unreachable":
                return
            delay = min(delay * 2, RETRY_MAX_SECONDS)

    async def _refresh_macs(self) -> None:
        """Caches every MAC the TV reports, on every successful connection.

        **Both interfaces are kept, deliberately.** `connectionmanager/getinfo`
        reports `wiredInfo`, `wifiInfo` and `p2pInfo` unconditionally, with no
        `state`, `ipAddress` or `connected` field to say which one is actually
        carrying traffic -- a TV on wifi still reports a wired MAC for the
        empty Ethernet socket. So there is nothing here to choose *between*,
        and an earlier version of this that preferred `wiredInfo` sent every
        magic packet to a port with no cable in it. `power_on` wakes all of
        them instead; the spare packet is inert.

        `p2pInfo` is left out: that is Wi-Fi Direct, and it wakes nothing.

        Re-read on every connection rather than only when nothing is stored,
        so re-cabling the TV from wifi to Ethernet -- or a version of this
        backend that stored the wrong one -- corrects itself on the next
        connection instead of being wrong until somebody deletes the file.

        Best-effort and silent: a failure here just means Wake-on-LAN needs
        the address typed in by hand, not that the connection is broken.
        """
        if self._client is None:
            return
        try:
            info = await self._client.get_connection_info()
        except Exception as err:
            logger.debug("[lgtv:%s] could not read connection info: %s", self.device_id, _why(err))
            return

        found = []
        for section in ("wiredInfo", "wifiInfo"):
            mac = _normalise_mac(str((info.get(section) or {}).get("macAddress") or ""))
            if mac and mac not in found:
                found.append(mac)

        if found and found != self._cached_macs():
            logger.info("[lgtv:%s] wake-on-lan addresses: %s", self.device_id, ", ".join(found))
            self._save_key_file(macs=found)

    async def _refresh_names(self) -> None:
        """Caches input and app labels, so picked commands read as they do on the TV.

        A failure here is deliberately quiet, the same as Home Assistant's own
        name cache: the names are cosmetic, and `commands()` falls back to the
        raw id.
        """
        if self._client is None:
            return
        try:
            inputs = await self._client.get_inputs() or []
            apps = await self._client.get_apps() or []
        except Exception as err:
            logger.debug("[lgtv:%s] could not refresh names: %s", self.device_id, _why(err))
            return
        names: Dict[str, str] = {}
        for item in inputs:
            input_id = str(item.get("id") or "").strip()
            if input_id:
                names[f"input:{input_id}"] = str(item.get("label") or input_id)
        for item in apps:
            app_id = str(item.get("id") or "").strip()
            if app_id:
                names[f"app:{app_id}"] = str(item.get("title") or app_id)
        self._names = names

    # -- commands ---------------------------------------------------------

    async def commands(self) -> List[Command]:
        commands = list(COMMANDS)
        for entity_id in self.exposed:
            kind, _, ident = entity_id.partition(":")
            name = self._names.get(entity_id) or ident.replace("_", " ").replace(".", " ").title()
            if kind == "input":
                commands.append(
                    Command(name=entity_id, label=f"Input: {name}", description=f"Switches to {ident}.")
                )
            elif kind == "app":
                commands.append(
                    Command(name=entity_id, label=f"App: {name}", description=f"Launches {ident}.")
                )
        return commands

    def suggested_bindings(self) -> Dict[str, str]:
        return dict(SUGGESTED_BINDINGS)

    def focus_for(self, command: str) -> None:
        # Deliberately never takes the focus, the same as the Android TV
        # backend and for the same reason: pressing Volume Up on the TV
        # should not steal the SmartHome +/- keys from whatever light was
        # last touched.
        return None

    async def _ready(self) -> Any:
        """The connected client, reconnecting once if the socket has dropped."""
        if self._client is None:
            raise BackendError(f"device '{self.device_id}' is not usable: {self._detail}")
        if not self._client.is_connected():
            await self._try_connect()
        if not self._client.is_connected():
            raise BackendError(f"device '{self.device_id}' is {self._state}: {self._detail}")
        return self._client

    async def _call(self, coro_fn, what: str) -> None:
        client = await self._ready()
        try:
            await coro_fn(client)
        except Exception as err:
            raise BackendError(f"{self.device_id}: {what} failed ({_why(err)})") from err

    async def _power_on(self) -> None:
        macs = self._macs()
        if not macs:
            raise BackendError(
                f"device '{self.device_id}' has no MAC address on file yet -- connect once "
                "with the TV already on, or set one in the device form"
            )
        port = int(self.config.get("wol_port") or DEFAULT_WOL_PORT)
        broadcast = str(self.config.get("wol_broadcast") or DEFAULT_WOL_BROADCAST).strip()
        await asyncio.to_thread(_send_magic_packets, macs, port, broadcast)

    async def send(self, command: str, params: Optional[Dict[str, Any]] = None) -> None:
        params = params or {}

        if command == "power_on":
            await self._power_on()
            return
        if command in BUTTON_NAMES:
            webos_name = BUTTON_NAMES[command]
            await self._call(lambda c, n=webos_name: c.button(n), f"button {webos_name}")
            return

        if command == "button":
            name = str(params.get("name") or "").strip()
            if not name:
                raise BackendError("the 'button' command needs a name parameter")
            await self._call(lambda c: c.button(name), f"button {name}")
            return
        if command == "power_off":
            await self._call(lambda c: c.power_off(), "power off")
            return
        if command == "screen_on":
            await self._call(lambda c: c.set_screen_state(True), "screen on")
            return
        if command == "screen_off":
            await self._call(lambda c: c.set_screen_state(False), "screen off")
            return
        if command == "set_volume":
            level = _int_param(params, "level", 0, 100, command)
            await self._call(lambda c: c.set_volume(level), f"set volume {level}")
            return
        if command == "set_channel":
            channel = str(params.get("channel") or "").strip()
            if not channel:
                raise BackendError("the 'set_channel' command needs a channel parameter")
            await self._call(lambda c: c.set_channel(channel), f"set channel {channel}")
            return
        if command == "set_input":
            input_id = str(params.get("input") or "").strip()
            if not input_id:
                raise BackendError("the 'set_input' command needs an input parameter")
            await self._call(lambda c: c.set_input(input_id), f"switch to {input_id}")
            return
        if command == "launch_app":
            app = str(params.get("app") or "").strip()
            if not app:
                raise BackendError("the 'launch_app' command needs an app parameter")
            await self._call(lambda c: c.launch_app(app), f"launch {app}")
            return
        if command == "sound_output":
            output = str(params.get("output") or "").strip()
            if not output:
                raise BackendError("the 'sound_output' command needs an output parameter")
            await self._call(lambda c: c.change_sound_output(output), f"sound output {output}")
            return
        if command == "toast":
            message = str(params.get("message") or "")
            await self._call(lambda c: c.send_message(message), "show message")
            return
        if command in ("play", "pause", "stop", "rewind", "fast_forward"):
            await self._call(lambda c, m=command: getattr(c, m)(), command)
            return

        kind, sep, ident = command.partition(":")
        if sep and kind in ("input", "app") and command in self.exposed:
            if kind == "input":
                await self._call(lambda c: c.set_input(ident), f"switch to {ident}")
            else:
                await self._call(lambda c: c.launch_app(ident), f"launch {ident}")
            return

        raise BackendError(f"device '{self.device_id}' has no command '{command}'")

    # -- catalogue ----------------------------------------------------------

    async def entities(self) -> List[Dict[str, Any]]:
        client = await self._ready()
        found: List[Dict[str, Any]] = []
        try:
            inputs = await client.get_inputs() or []
        except Exception as err:
            raise BackendError(f"could not list inputs ({_why(err)})") from err
        for item in inputs:
            input_id = str(item.get("id") or "").strip()
            if not input_id:
                continue
            found.append(
                {
                    "entity_id": f"input:{input_id}",
                    "name": str(item.get("label") or input_id),
                    "domain": "input",
                    "state": "connected" if item.get("connected") else "disconnected",
                    "controllable": True,
                }
            )
        try:
            apps = await client.get_apps() or []
        except Exception as err:
            raise BackendError(f"could not list apps ({_why(err)})") from err
        for item in apps:
            app_id = str(item.get("id") or "").strip()
            if not app_id:
                continue
            found.append(
                {
                    "entity_id": f"app:{app_id}",
                    "name": str(item.get("title") or app_id),
                    "domain": "app",
                    "state": "",
                    "controllable": True,
                }
            )
        return found

    # -- health -------------------------------------------------------------

    async def health(self) -> Health:
        if self._client is None or self._state != "connected" or not self._client.is_connected():
            return Health(ok=False, detail=self._detail or self._state)
        state = self._client.tv_state
        parts = ["on" if state.is_on else "standby"]
        if state.current_app_id:
            app = state.apps.get(state.current_app_id) or {}
            parts.append(str(app.get("title") or state.current_app_id))
        if state.volume is not None:
            parts.append(f"vol {state.volume}")
        return Health(ok=True, detail=" · ".join(parts))

    # -- state ----------------------------------------------------------------

    async def readable(self) -> List[StateTarget]:
        return [
            StateTarget(target="power", label="Power", values=("on", "standby")),
            StateTarget(target="app", label="Current app"),
            StateTarget(target="volume", label="Volume"),
            StateTarget(target="muted", label="Muted", values=("true", "false")),
        ]

    async def read_state(self, target: str) -> str:
        """Answered from the client's own cached state -- no round trip to the
        TV, the same reason `health()` reads it this way. A TV that is off or
        unreachable still answers `power` as `standby`; every other target
        needs a live connection to mean anything.
        """
        if target == "power":
            connected = self._client is not None and self._client.is_connected()
            return "on" if connected and self._client.tv_state.is_on else "standby"
        if self._client is None or not self._client.is_connected():
            raise BackendError(f"device '{self.device_id}' is {self._state}: {self._detail}")
        state = self._client.tv_state
        if target == "app":
            return str(state.current_app_id or "")
        if target == "volume":
            return "" if state.volume is None else str(state.volume)
        if target == "muted":
            return "" if state.muted is None else ("true" if state.muted else "false")
        raise BackendError(f"device '{self.device_id}' has no state '{target}'")

    # -- pairing --------------------------------------------------------------

    async def pair_start(self) -> str:
        if WebOsClient is None:
            raise BackendError(f"aiowebostv is not installed ({IMPORT_ERROR})")
        if not self.host:
            raise BackendError(f"device '{self.device_id}' has no address set")
        if self._pair_task is not None and not self._pair_task.done():
            self._pair_task.cancel()
        self._pair_client = None
        self._pair_task = asyncio.create_task(self._register_until_accepted())
        self._set_state("pairing", "waiting for the prompt to be accepted on the TV")
        return (
            "A connection prompt will appear on the TV -- accept it with the TV's own "
            "remote, then come back here. If you miss it, it keeps reappearing for a "
            f"while (up to {int(PAIR_RETRY_WINDOW)}s)."
        )

    async def _register_until_accepted(self) -> str:
        """Repeatedly registers a fresh client until the TV's prompt is accepted.

        See the module docstring: one `connect()` call folds registration and
        the human-approval wait into a window shorter than a person actually
        needs, so this keeps opening a fresh attempt -- each one re-showing
        the TV's prompt -- until one succeeds or `PAIR_RETRY_WINDOW` runs out.
        """
        deadline = time.monotonic() + PAIR_RETRY_WINDOW
        last_err: Optional[BaseException] = None
        while time.monotonic() < deadline:
            client = WebOsClient(self.host, client_key=None)
            try:
                await asyncio.wait_for(client.connect(), timeout=PAIR_ATTEMPT_TIMEOUT)
            except WebOsTvPairError:
                # An explicit decline, not a missed window -- stop asking.
                raise
            except Exception as err:  # noqa: BLE001 - every other cause just means "try again"
                last_err = err
                await client.disconnect()
                await asyncio.sleep(PAIR_RETRY_GAP)
                continue
            self._pair_client = client
            return str(client.client_key)
        raise BackendError(
            f"no response from the TV within {int(PAIR_RETRY_WINDOW)}s"
            + (f" ({_why(last_err)})" if last_err else "")
        )

    async def pair_finish(self, code: str) -> None:
        # `code` is unused: webOS asks for a press on the TV's own remote,
        # not something typed back here -- see `pair_input_label`.
        if self._pair_task is None:
            raise BackendError(f"device '{self.device_id}': pairing was not started")
        try:
            key = await self._pair_task
        except WebOsTvPairError as err:
            self._set_state("unpaired", "the prompt was declined on the TV")
            raise BackendError(f"pairing was declined on the TV ({_why(err)})") from err
        except Exception as err:
            self._set_state("unpaired", f"pairing failed: {_why(err)}")
            raise BackendError(f"pairing failed: {_why(err)}") from err
        finally:
            self._pair_task = None

        self._save_key_file(client_key=key)
        client, self._pair_client = self._pair_client, None
        if client is not None:
            await client.disconnect()

        await self.connect()
        if self._state != "connected":
            raise BackendError(f"paired, but could not connect: {self._detail}")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


async def discover(timeout: float = 3.0) -> List[Dict[str, Any]]:
    """LG webOS TVs announcing themselves over SSDP.

    Same job and the same failure mode as the other network backends'
    discovery: nothing answering returns an empty list rather than raising,
    because the address field is still there to type into.
    """
    try:
        locations = await _ssdp.search(timeout, _SEARCH_TARGETS)
    except OSError as err:  # pragma: no cover - no usable network interface
        logger.warning("Could not start discovery: %s", err)
        return []

    found: Dict[str, Dict[str, Any]] = {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for location in locations:
                host = urlparse(location).hostname
                if not host or host in found:
                    continue
                try:
                    response = await client.get(location)
                    response.raise_for_status()
                except httpx.HTTPError as err:
                    logger.debug("LG webOS discovery: %s did not answer (%s)", location, err)
                    continue
                fields = {
                    name.lower(): value for name, value in _ssdp.DESC_FIELD.findall(response.text)
                }
                manufacturer = fields.get("manufacturer", "").lower()
                if not any(known in manufacturer for known in _MANUFACTURERS):
                    continue
                name = fields.get("friendlyname") or fields.get("modelname") or host
                found[host] = {
                    "name": name,
                    "host": host,
                    "model": fields.get("modelname", ""),
                }
    except Exception as err:  # pragma: no cover - defensive
        logger.warning("Discovery failed: %s", err)

    return sorted(found.values(), key=lambda entry: entry["name"])
