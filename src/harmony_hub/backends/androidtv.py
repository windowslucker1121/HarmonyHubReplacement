"""Android TV and Google TV, over the Remote v2 protocol.

This is the protocol the Google TV phone app speaks, served by the Android TV
Remote Service that ships preinstalled on an Nvidia Shield and on essentially
every Android TV. That matters more than it sounds: the alternative is ADB,
which needs developer options, network debugging, and an RSA fingerprint
dialog accepted on the TV. Here the whole setup is one six-digit code shown on
screen, once, and a client certificate kept from then on.

Two ports are involved -- 6467 to pair, 6466 to control -- and both are TLS.
Pairing is a certificate exchange the user confirms with the code, so the
certificate is what authenticates every later session. It lives on disk rather
than in `hub_config.json`, which keeps secret material out of the device form
and means pairing never has to write configuration at all.

The one structural requirement is that `connect()` never raises: the engine
only registers a backend whose `connect()` returned, and a backend that is not
registered cannot be reached by the pairing routes -- an unpaired device would
be impossible to pair. So a TV that is unpaired or asleep starts anyway and
says so through `health()`, which is also the honest answer: "the Shield is in
standby" is a normal state, not a configuration error.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import Backend, BackendError, Command, Health, Pairable, register

logger = logging.getLogger("HUB.androidtv")

try:
    from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

    IMPORT_ERROR = ""
except ImportError as err:  # pragma: no cover - only without the dependency
    AndroidTVRemote = None
    # Placeholders so the `except` clauses below stay valid. Nothing reaches
    # them: every path checks `AndroidTVRemote is None` first.
    CannotConnect = ConnectionClosed = InvalidAuth = OSError
    IMPORT_ERROR = str(err)

#: Where paired certificates go, relative to the working directory -- the same
#: convention `hub_config.json` and `buttons.json` already follow.
DEFAULT_CREDENTIALS_DIR = "credentials"

#: mDNS service the Android TV Remote Service advertises itself under.
SERVICE_TYPE = "_androidtvremote2._tcp.local."

#: Long enough for a TLS handshake over wifi, short enough to stay inside the
#: engine's 15s action timeout with room for the send itself.
CONNECT_TIMEOUT = 5.0

RETRY_MIN_SECONDS = 2.0
RETRY_MAX_SECONDS = 60.0

#: The two `key` directions that inject a held keypress rather than a tap,
#: meant to be paired across a button's press and release to produce a real
#: Android long press -- the only way apps like YouTube open their
#: long-press context menu instead of acting on the focused item at once.
DIRECTIONS = ("SHORT", "START_LONG", "END_LONG")

#: A `START_LONG` with no matching `END_LONG` -- a dropped connection
#: mid-hold, a lost release event -- would otherwise leave the key down on
#: the TV forever. Nothing on a physical remote is legitimately held longer.
MAX_HOLD_SECONDS = 5.0


def _why(err: BaseException) -> str:
    """A readable reason for an exception that may carry no message at all.

    The client raises several of its errors bare -- `raise CannotConnect from
    exc` -- and `asyncio.TimeoutError` has never had a message. Interpolating
    those straight into a health line produces an empty pair of brackets,
    which tells the user nothing about what went wrong.
    """
    return str(err) or type(err).__name__


# --------------------------------------------------------------------------
# Command table
# --------------------------------------------------------------------------

# name, Android key code, label, safe to repeat while the button is held
_KEYS: Tuple[Tuple[str, str, str, bool], ...] = (
    # Navigation
    ("dpad_up", "DPAD_UP", "Up", True),
    ("dpad_down", "DPAD_DOWN", "Down", True),
    ("dpad_left", "DPAD_LEFT", "Left", True),
    ("dpad_right", "DPAD_RIGHT", "Right", True),
    ("select", "DPAD_CENTER", "Select", False),
    ("back", "BACK", "Back", False),
    ("home", "HOME", "Home", False),
    ("menu", "MENU", "Menu", False),
    ("info", "INFO", "Info", False),
    ("guide", "GUIDE", "Guide", False),
    ("search", "SEARCH", "Search", False),
    ("assistant", "ASSIST", "Assistant", False),
    ("settings", "SETTINGS", "Settings", False),
    ("captions", "CAPTIONS", "Subtitles", False),
    ("audio_track", "MEDIA_AUDIO_TRACK", "Audio track", False),
    ("delete", "DEL", "Delete", True),
    ("enter", "ENTER", "Enter", False),
    ("tv", "TV", "TV", False),
    # Volume
    ("volume_up", "VOLUME_UP", "Volume up", True),
    ("volume_down", "VOLUME_DOWN", "Volume down", True),
    ("mute", "VOLUME_MUTE", "Mute", False),
    # Transport
    ("play_pause", "MEDIA_PLAY_PAUSE", "Play / pause", False),
    ("play", "MEDIA_PLAY", "Play", False),
    ("pause", "MEDIA_PAUSE", "Pause", False),
    ("stop", "MEDIA_STOP", "Stop", False),
    ("next", "MEDIA_NEXT", "Next", False),
    ("previous", "MEDIA_PREVIOUS", "Previous", False),
    ("record", "MEDIA_RECORD", "Record", False),
    ("rewind", "MEDIA_REWIND", "Rewind", True),
    ("fast_forward", "MEDIA_FAST_FORWARD", "Fast forward", True),
    # Channels
    ("channel_up", "CHANNEL_UP", "Channel up", True),
    ("channel_down", "CHANNEL_DOWN", "Channel down", True),
    # Colour keys
    ("red", "PROG_RED", "Red", False),
    ("green", "PROG_GREEN", "Green", False),
    ("yellow", "PROG_YELLOW", "Yellow", False),
    ("blue", "PROG_BLUE", "Blue", False),
    # Digits
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
    # Raw power. `power_on` and `power_off` are almost always the better
    # choice; this is here for a remote whose power button should just toggle.
    ("power", "POWER", "Power (toggle)", False),
)

KEY_CODES: Dict[str, str] = {name: code for name, code, _label, _repeat in _KEYS}

#: Buttons from `buttons.json` mapped to the command that suits them. The four
#: activity keys and the six SmartHome keys are deliberately absent: those
#: belong to scene actions and to lighting, not to the television.
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
    "keypad": "delete",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "mute": "mute",
    "channel_up": "channel_up",
    "channel_down": "channel_down",
    "play": "play",
    "pause": "pause",
    "stop": "stop",
    "record": "record",
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


#: Offered on every named key command, not just the raw `key` one -- a
#: button bound to "Select" should be able to ask for a long press without
#: falling back to typing "DPAD_CENTER" into the raw command instead.
_DIRECTION_PARAM: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "direction": {
            "type": "string",
            "title": "Direction",
            "enum": list(DIRECTIONS),
            "default": "SHORT",
            "description": "SHORT taps. START_LONG holds the key down until a matching "
            "END_LONG lets go -- bind the pair to a button's press and release for a "
            "real long press, the way YouTube's 'Watch later' menu expects one.",
        }
    },
}


def _build_commands() -> List[Command]:
    """The fixed command set. An Android TV's capabilities do not vary."""
    commands = [
        Command(
            name=name,
            label=label,
            description=f"KEYCODE_{code}",
            repeatable=repeatable,
            params=_DIRECTION_PARAM,
        )
        for name, code, label, repeatable in _KEYS
    ]
    commands += [
        Command(
            name="power_on",
            label="Power on",
            description="Sends POWER only if the device is currently off.",
        ),
        Command(
            name="power_off",
            label="Power off",
            description="Sends POWER only if the device is currently on.",
        ),
        Command(
            name="launch_app",
            label="Launch app",
            description="Opens a deep link or a package name, e.g. https://www.netflix.com "
            "or com.plexapp.android.",
            params={
                "type": "object",
                "required": ["app"],
                "properties": {"app": {"type": "string", "title": "App link or package name"}},
            },
        ),
        Command(
            name="text",
            label="Type text",
            description="Types into whatever field has focus. Needs text input enabled.",
            params={
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string", "title": "Text"}},
            },
        ),
        Command(
            name="key",
            label="Raw key code",
            description="Any Android key code, for the few this list does not name.",
            params={
                "type": "object",
                "required": ["key_code"],
                "properties": {
                    "key_code": {
                        "type": "string",
                        "title": "Key code",
                        "description": "With or without the KEYCODE_ prefix.",
                    },
                    "direction": {
                        "type": "string",
                        "title": "Direction",
                        "enum": list(DIRECTIONS),
                        "default": "SHORT",
                    },
                },
            },
        ),
        Command(
            name="hold",
            label="Long press",
            description="Holds a key down and releases it after a fixed delay -- for a "
            "scene macro, or a button with no separate release event to pair "
            "START_LONG/END_LONG against.",
            params={
                "type": "object",
                "properties": {
                    "key_code": {
                        "type": "string",
                        "title": "Key code",
                        "description": "With or without the KEYCODE_ prefix.",
                        "default": "DPAD_CENTER",
                    },
                    "hold_secs": {
                        "type": "number",
                        "title": "Hold for (seconds)",
                        "default": 0.5,
                        "minimum": 0.1,
                        "maximum": MAX_HOLD_SECONDS,
                    },
                },
            },
        ),
    ]
    return commands


COMMANDS: List[Command] = _build_commands()


# --------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------


@register
class AndroidTvBackend(Backend, Pairable):
    """One Android TV device -- an Nvidia Shield, a Chromecast, a smart TV."""

    name = "androidtv"
    label = "Android TV / Google TV"
    description = (
        "Nvidia Shield, Chromecast with Google TV, or any Android TV, over the same "
        "protocol the Google TV app uses. No developer mode and no ADB: pairing is one "
        "code shown on the screen."
    )
    discover_field = "host"

    def __init__(self, device_id: str, config: Dict[str, Any]) -> None:
        super().__init__(device_id, config)
        self._remote: Optional[Any] = None
        self._retry: Optional[asyncio.Task] = None
        self._watching = False
        self._state = "stopped"
        self._detail = "not started"
        #: Key codes currently down via START_LONG, each with a watchdog
        #: task that releases it if the matching END_LONG never arrives.
        self._held: Dict[str, asyncio.Task] = {}

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
                    "description": "IP address or hostname. Give it a fixed address in the "
                    "router: the connection drops when it moves.",
                    "default": "",
                },
                "client_name": {
                    "type": "string",
                    "title": "Client name",
                    "description": "Shown on the device while pairing, and in its list of "
                    "paired remotes afterwards.",
                    "default": "Harmony Hub",
                },
                "api_port": {"type": "integer", "title": "Control port", "default": 6466},
                "pair_port": {"type": "integer", "title": "Pairing port", "default": 6467},
                "enable_ime": {
                    "type": "boolean",
                    "title": "Enable text input",
                    "description": "Needed to type text and to report the running app. "
                    "Turn off if the device misbehaves with it.",
                    "default": True,
                },
                "cert_dir": {
                    "type": "string",
                    "title": "Certificate directory",
                    "description": "Where the paired certificate is kept. Blank means "
                    f"'{DEFAULT_CREDENTIALS_DIR}'.",
                    "default": "",
                },
            },
        }

    @property
    def host(self) -> str:
        return str(self.config.get("host") or "").strip()

    def _cert_paths(self) -> Tuple[Path, Path]:
        directory = Path(str(self.config.get("cert_dir") or "").strip() or DEFAULT_CREDENTIALS_DIR)
        return (
            directory / f"androidtv_{self.device_id}.crt",
            directory / f"androidtv_{self.device_id}.key",
        )

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Builds the client and tries once to reach the device.

        Never raises. Every failure becomes a state that `health()` explains
        and, where retrying could help, a background task that keeps trying.
        """
        await self.close()

        if AndroidTVRemote is None:
            self._set_state("unavailable", f"androidtvremote2 is not installed ({IMPORT_ERROR})")
            return
        if not self.host:
            self._set_state("unconfigured", "no address set")
            return

        certfile, keyfile = self._cert_paths()
        certfile.parent.mkdir(parents=True, exist_ok=True)

        self._remote = AndroidTVRemote(
            client_name=str(self.config.get("client_name") or "Harmony Hub"),
            certfile=str(certfile),
            keyfile=str(keyfile),
            host=self.host,
            api_port=int(self.config.get("api_port") or 6466),
            pair_port=int(self.config.get("pair_port") or 6467),
            enable_ime=bool(self.config.get("enable_ime", True)),
        )
        self._watching = False
        await self._remote.async_generate_cert_if_missing()
        await self._try_connect()

    async def close(self) -> None:
        if self._retry is not None:
            self._retry.cancel()
            self._retry = None
        for task in self._held.values():
            task.cancel()
        self._held.clear()
        if self._remote is not None:
            self._remote.disconnect()
            self._remote = None
        self._watching = False
        self._set_state("stopped", "not started")

    def _set_state(self, state: str, detail: str) -> None:
        if (state, detail) != (self._state, self._detail):
            logger.info(
                "[androidtv:%s] %s%s", self.device_id, state, f" -- {detail}" if detail else ""
            )
        self._state = state
        self._detail = detail

    async def _try_connect(self) -> str:
        """One connection attempt. Returns the state it left behind."""
        try:
            await asyncio.wait_for(self._remote.async_connect(), timeout=CONNECT_TIMEOUT)
        except InvalidAuth:
            # Retrying cannot help: the device refuses us until a human
            # confirms a code on the screen.
            self._set_state("unpaired", "not paired -- pair this device to use it")
        except (CannotConnect, ConnectionClosed, asyncio.TimeoutError, OSError) as err:
            self._set_state("unreachable", f"cannot reach {self.host} ({_why(err)})")
            self._schedule_retry()
        except Exception as err:  # pragma: no cover - defensive
            self._set_state("error", str(err))
        else:
            self._set_state("connected", "")
            # From here the library owns reconnection -- but only while it has
            # a live protocol, which is why the first attempt has to be ours.
            #
            # Once per client, not once per attempt: `keep_reconnecting` starts
            # a fresh task each call without stopping the previous one, and the
            # callback list only ever grows. A device that drops often would
            # otherwise end up with a pile of both.
            if not self._watching:
                self._watching = True
                self._remote.keep_reconnecting(self._on_invalid_auth)
                self._remote.add_is_available_updated_callback(self._on_availability)
        return self._state

    def _on_invalid_auth(self) -> None:
        self._set_state("unpaired", "the device revoked this pairing -- pair it again")

    def _on_availability(self, available: bool) -> None:
        if available:
            self._set_state("connected", "")
        elif self._state == "connected":
            self._set_state("unreachable", f"lost the connection to {self.host}, retrying")

    def _schedule_retry(self) -> None:
        if self._retry is not None and not self._retry.done():
            return
        self._retry = asyncio.create_task(self._retry_until_connected())

    async def _retry_until_connected(self) -> None:
        """Keeps trying a device that was not there when the hub started.

        The library's own reconnect loop only runs once a connection has been
        established at least once, so a TV that was asleep at startup would
        otherwise stay dead until the configuration was saved again.
        """
        delay = RETRY_MIN_SECONDS
        while True:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self._remote is None:
                return
            if await self._try_connect() != "unreachable":
                return
            delay = min(delay * 2, RETRY_MAX_SECONDS)

    # -- commands ---------------------------------------------------------

    async def commands(self) -> List[Command]:
        return COMMANDS

    def suggested_bindings(self) -> Dict[str, str]:
        return dict(SUGGESTED_BINDINGS)

    @property
    def _connected(self) -> bool:
        # `is_on` is None exactly when there is no live protocol behind it.
        return self._remote is not None and self._remote.is_on is not None

    async def _ready(self):
        """The connected client, reconnecting once if the socket has dropped."""
        if self._remote is None:
            raise BackendError(f"device '{self.device_id}' is not usable: {self._detail}")
        if not self._connected:
            await self._try_connect()
        if not self._connected:
            raise BackendError(f"device '{self.device_id}' is {self._state}: {self._detail}")
        return self._remote

    async def send(self, command: str, params: Optional[Dict[str, Any]] = None) -> None:
        params = params or {}

        if command in KEY_CODES:
            await self._send_key(KEY_CODES[command], str(params.get("direction") or "SHORT"))
        elif command in ("power_on", "power_off"):
            await self._set_power(command == "power_on")
        elif command == "key":
            key_code = str(params.get("key_code") or "").strip()
            if not key_code:
                raise BackendError("the 'key' command needs a key_code parameter")
            await self._send_key(key_code, str(params.get("direction") or "SHORT"))
        elif command == "hold":
            key_code = str(params.get("key_code") or "DPAD_CENTER").strip()
            hold_secs = float(params.get("hold_secs") or 0.5)
            await self._hold_key(key_code, hold_secs)
        elif command == "launch_app":
            app = str(params.get("app") or "").strip()
            if not app:
                raise BackendError("the 'launch_app' command needs an app parameter")
            await self._call(lambda remote: remote.send_launch_app_command(app), f"launch {app}")
        elif command == "text":
            text = str(params.get("text") or "")
            await self._call(lambda remote: remote.send_text(text), "type text")
        else:
            raise BackendError(f"device '{self.device_id}' has no command '{command}'")

    async def _send_key(self, key_code: str, direction: str = "SHORT") -> None:
        if direction not in DIRECTIONS:
            raise BackendError(f"unknown key direction '{direction}'")
        if direction == "START_LONG" and key_code in self._held:
            # A second START_LONG on a key already down would otherwise
            # orphan the first hold's watchdog and its eventual END_LONG.
            await self._send_key(key_code, "END_LONG")
        await self._call(
            lambda remote: remote.send_key_command(key_code, direction), f"key {key_code}"
        )
        if direction == "START_LONG":
            self._held[key_code] = asyncio.create_task(self._watchdog(key_code))
        elif direction == "END_LONG":
            self._cancel_watchdog(key_code)

    async def _hold_key(self, key_code: str, hold_secs: float) -> None:
        """Sends one atomic long press: down, wait, up.

        For a binding with no separate release to pair START_LONG against --
        a scene macro, or a button whose release the remote reports
        unreliably. Costs `hold_secs` of latency before the command returns,
        which is why a press/release pair is the better choice whenever the
        binding has one available.
        """
        await self._send_key(key_code, "START_LONG")
        await asyncio.sleep(hold_secs)
        await self._send_key(key_code, "END_LONG")

    def _cancel_watchdog(self, key_code: str) -> None:
        task = self._held.pop(key_code, None)
        if task is not None:
            task.cancel()

    async def _watchdog(self, key_code: str) -> None:
        """Releases a key whose matching END_LONG never arrived.

        A dropped connection between press and release, or a lost release
        event, would otherwise leave this key down on the TV indefinitely.
        """
        try:
            await asyncio.sleep(MAX_HOLD_SECONDS)
        except asyncio.CancelledError:
            return
        self._held.pop(key_code, None)
        logger.warning(
            "[androidtv:%s] key %s held past %.0fs with no release -- releasing it",
            self.device_id,
            key_code,
            MAX_HOLD_SECONDS,
        )
        try:
            await self._call(
                lambda remote: remote.send_key_command(key_code, "END_LONG"), f"key {key_code}"
            )
        except BackendError:
            pass  # the connection is presumably already gone; nothing more to do

    async def _call(self, action, what: str) -> None:
        remote = await self._ready()
        try:
            action(remote)
        except Exception as err:
            raise BackendError(f"{self.device_id}: {what} failed ({err})") from err

    async def _set_power(self, on: bool) -> None:
        """Powers on or off, but only when that would change something.

        POWER is a toggle, so a scene that switched an already-on TV 'on'
        would switch it off. Reading the state first is what makes this device
        safe to use under a managed power policy.
        """
        remote = await self._ready()
        if remote.is_on == on:
            return
        await self._send_key("POWER")

    async def health(self) -> Health:
        if self._state != "connected" or self._remote is None:
            return Health(ok=False, detail=self._detail or self._state)
        parts = ["on" if self._remote.is_on else "standby"]
        if self._remote.current_app:
            parts.append(self._remote.current_app)
        return Health(ok=True, detail=" · ".join(parts))

    # -- pairing ----------------------------------------------------------

    async def pair_start(self) -> str:
        if self._remote is None:
            raise BackendError(f"device '{self.device_id}' is not usable: {self._detail}")
        try:
            await self._remote.async_start_pairing()
        except Exception as err:
            raise BackendError(f"could not start pairing with {self.host}: {_why(err)}") from err
        self._set_state("pairing", "waiting for the code shown on the device")
        return "Enter the six-digit code shown on the device."

    async def pair_finish(self, code: str) -> None:
        if self._remote is None:
            raise BackendError(f"device '{self.device_id}' is not usable: {self._detail}")
        try:
            await self._remote.async_finish_pairing(code.strip())
        except InvalidAuth as err:
            self._set_state("unpaired", "the code was not accepted")
            raise BackendError("the code was not accepted -- start pairing again") from err
        except Exception as err:
            self._set_state("unpaired", f"pairing failed: {_why(err)}")
            raise BackendError(f"pairing failed: {_why(err)}") from err

        # Pairing leaves the client disconnected, so this is a fresh start
        # rather than a resumed session.
        await self.connect()
        if self._state != "connected":
            raise BackendError(f"paired, but could not connect: {self._detail}")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


async def discover(timeout: float = 3.0) -> List[Dict[str, Any]]:
    """Android TV devices advertising themselves on the local network.

    Saves typing an IP address, and finds the device again after DHCP has
    moved it. A network where mDNS does not work returns nothing rather than
    failing -- the address field is still there to type into.
    """
    try:
        from zeroconf import IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError as err:  # pragma: no cover - only without the dependency
        logger.warning("Discovery needs zeroconf, which is not installed (%s)", err)
        return []

    names: List[str] = []

    def on_change(zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Added:
            names.append(name)

    try:
        azc = AsyncZeroconf()
    except Exception as err:  # pragma: no cover - no usable network interface
        logger.warning("Could not start discovery: %s", err)
        return []

    found: List[Dict[str, Any]] = []
    browser = AsyncServiceBrowser(azc.zeroconf, SERVICE_TYPE, handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
        for name in names:
            info = AsyncServiceInfo(SERVICE_TYPE, name)
            if not await info.async_request(azc.zeroconf, 3000):
                continue
            # IPv4 only: an Android TV reached over a temporary IPv6 address
            # stops answering as soon as that address is rotated.
            addresses = info.parsed_addresses(IPVersion.V4Only)
            if not addresses:
                continue
            found.append(
                {
                    "name": name.removesuffix("." + SERVICE_TYPE) or name,
                    "host": addresses[0],
                    "port": info.port or 6466,
                }
            )
    except Exception as err:  # pragma: no cover - defensive
        logger.warning("Discovery failed: %s", err)
    finally:
        await browser.async_cancel()
        await azc.async_close()

    return sorted(found, key=lambda entry: entry["name"])
