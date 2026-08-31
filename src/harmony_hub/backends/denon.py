"""Denon and Marantz AV receivers, over their local control protocol.

The receiver in the middle of an AV chain is the one box that genuinely has
to work: it owns the volume. Denon has spoken the same short-string control
protocol since the RS-232 days -- `MVUP`, `PWON`, `SISAT/CBL` -- and every
network model since has carried it over the LAN unchanged. There is no cloud,
no account and no pairing, which is why this backend is smaller than the two
that came before it.

Two decisions shape this file.

**One vocabulary, two transports.** The same command strings travel either
over telnet on port 23 or as an HTTP query on port 8080, and which of those a
given unit answers on turns out to vary by model and by firmware -- some units
answer only one. So the protocol strings are written once and the transport is
a field in the device form rather than a fork in the command table. HTTP is the
default because it is stateless and because a receiver accepts only *one*
telnet client at a time: a hub that held that connection open would lock out
the Denon phone app and Home Assistant's own integration. For the same reason
the telnet path opens a connection per command and closes it again, trading a
few milliseconds for not owning the only slot.

**Inputs are chosen; everything else is fixed.** A receiver has thirteen
selectable sources and a living room uses three of them, so the inputs come
from the same `entities` config key -- and therefore the same "Choose entities"
picker -- that Home Assistant uses. Power, volume, mute, surround, the on-screen
menu and Zone 2 exist on every unit and are always offered, because there is
nothing there to choose.

One thing to know before changing it: **`Network Control` must be `Always On`**
on the receiver. Denon's default powers the network interface down in standby,
which drops the unit off the network entirely -- so it cannot be woken remotely,
and every integration against it appears simply dead. That is the first thing
to check when this backend reports a receiver as unreachable.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx

from . import Backend, BackendError, Command, Health, Readable, StateTarget, register
from . import _ssdp

logger = logging.getLogger("HUB.denon")

#: Where the HTTP transport sends commands. Denon has used 8080 for this since
#: the 2016 models; older units answer on 80, which is why it is a field.
DEFAULT_HTTP_PORT = 8080

DEFAULT_TELNET_PORT = 23

DEFAULT_TIMEOUT = 5.0

#: How long a health answer is reused. The device list polls, and a receiver
#: does not change its mind about being switched on ten times a second.
HEALTH_TTL = 10.0

#: Denon asks for at least 50ms between commands and drops what arrives sooner.
#: This is a floor on the wire, not a repeat rate -- how fast a held button
#: repeats is the user's existing global and per-button setting, untouched here.
MIN_COMMAND_GAP = 0.05

#: How long to wait for one line of a telnet reply. Only queries wait at all;
#: an ordinary command is written and the connection closed behind it.
REPLY_TIMEOUT = 1.5

#: A receiver answering a query is entitled to volunteer unrelated status
#: lines first, so a reply is read past a few of them before giving up.
REPLY_LINES = 8


# --------------------------------------------------------------------------
# Command table
# --------------------------------------------------------------------------

# name, protocol string, label, safe to repeat while the button is held
_FIXED: Tuple[Tuple[str, str, str, bool], ...] = (
    # Power. The protocol has discrete commands rather than a toggle, so a
    # scene can end with "off" without first having to know it was on.
    ("power_on", "PWON", "Power on", False),
    ("power_off", "PWSTANDBY", "Power off (standby)", False),
    ("main_zone_on", "ZMON", "Main zone on", False),
    ("main_zone_off", "ZMOFF", "Main zone off", False),
    # Volume
    ("volume_up", "MVUP", "Volume up", True),
    ("volume_down", "MVDOWN", "Volume down", True),
    ("mute_on", "MUON", "Mute", False),
    ("mute_off", "MUOFF", "Unmute", False),
    # Surround
    ("surround_movie", "MSMOVIE", "Surround — Movie", False),
    ("surround_music", "MSMUSIC", "Surround — Music", False),
    ("surround_game", "MSGAME", "Surround — Game", False),
    ("surround_direct", "MSDIRECT", "Surround — Direct", False),
    ("surround_pure_direct", "MSPURE DIRECT", "Surround — Pure Direct", False),
    ("surround_stereo", "MSSTEREO", "Surround — Stereo", False),
    ("surround_auto", "MSAUTO", "Surround — Auto", False),
    ("surround_multi_stereo", "MSMCH STEREO", "Surround — Multi Ch Stereo", False),
    ("surround_dolby", "MSDOLBY DIGITAL", "Surround — Dolby Digital", False),
    ("surround_dts", "MSDTS SURROUND", "Surround — DTS Surround", False),
    # The receiver's own on-screen menu.
    ("menu_on", "MNMEN ON", "Setup menu", False),
    ("menu_off", "MNMEN OFF", "Close setup menu", False),
    ("cursor_up", "MNCUP", "Up", True),
    ("cursor_down", "MNCDN", "Down", True),
    ("cursor_left", "MNCLT", "Left", True),
    ("cursor_right", "MNCRT", "Right", True),
    ("cursor_enter", "MNENT", "Enter", False),
    ("cursor_return", "MNRTN", "Back", False),
    ("info", "MNINF", "Info", False),
    ("option", "MNOPT", "Option", False),
    # Zone 2
    ("zone2_on", "Z2ON", "Zone 2 on", False),
    ("zone2_off", "Z2OFF", "Zone 2 off", False),
    ("zone2_volume_up", "Z2UP", "Zone 2 volume up", True),
    ("zone2_volume_down", "Z2DOWN", "Zone 2 volume down", True),
    ("zone2_mute_on", "Z2MUON", "Zone 2 mute", False),
    ("zone2_mute_off", "Z2MUOFF", "Zone 2 unmute", False),
    # Sleep
    ("sleep_off", "SLPOFF", "Cancel sleep timer", False),
)

PROTOCOL: Dict[str, str] = {name: string for name, string, _label, _repeat in _FIXED}

#: Selectable sources, as protocol token and the name the front panel uses for
#: it. The token is not always the label -- the socket marked CBL/SAT answers
#: to `SAT/CBL` -- which is exactly why nobody should be typing these by hand.
INPUTS: Tuple[Tuple[str, str], ...] = (
    ("SAT/CBL", "CBL/SAT"),
    ("DVD", "DVD"),
    ("BD", "Blu-ray"),
    ("GAME", "Game"),
    ("MPLAY", "Media Player"),
    ("TV", "TV Audio"),
    ("AUX1", "AUX1"),
    ("AUX2", "AUX2"),
    ("CD", "CD"),
    ("PHONO", "Phono"),
    ("TUNER", "Tuner"),
    ("NET", "HEOS Music"),
    ("BT", "Bluetooth"),
)

INPUT_LABELS: Dict[str, str] = dict(INPUTS)

#: Buttons from `buttons.json` mapped to the command that suits them.
#:
#: This overlaps the Android TV backend on volume and the arrows, which is
#: deliberate: the receiver is the thing that actually changes the volume in a
#: room that has one, and its setup menu needs the same arrows the television's
#: does. Suggestions are reviewed before they are applied and the mapper works
#: one device at a time, so an overlap costs a glance rather than a mistake.
SUGGESTED_BINDINGS: Dict[str, str] = {
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "mute": "mute",
    "up_arrow": "cursor_up",
    "down_arrow": "cursor_down",
    "left_arrow": "cursor_left",
    "right_arrow": "cursor_right",
    "enter": "cursor_enter",
    "keypad_enter": "cursor_enter",
    "ac_back": "cursor_return",
    "quit": "cursor_return",
    "application_menu_key": "menu_on",
    "consumer_0x01ff": "info",
}

#: Query string and status-document key for each value worth reading back.
#: The two transports read state completely differently -- telnet answers a
#: query, HTTP serves a document -- so both spellings live together here.
READABLE: Dict[str, Tuple[str, str]] = {
    "power": ("PW?", "Power"),
    "mute": ("MU?", "Mute"),
    "source": ("SI?", "InputFuncSelect"),
}

#: The surround mode is deliberately not in `READABLE` above: it has no HTTP
#: equivalent at all, confirmed against a real AVR-X2700H -- the Lite status
#: document `_read_status_document` reads does not carry it, the full
#: document 403s, and every `AppCommand.xml` variant answers an empty `<rx/>`.
#: `MS?` over telnet is the only way to read it back; see `_read_surround`.
SURROUND_QUERY = "MS?"

#: `<Power><value>ON</value></Power>` and its dozen siblings, which is the whole
#: shape of the status document. A regex rather than an XML parser because one
#: flat level of name/value pairs is all that is ever wanted from it.
_STATUS_VALUE = re.compile(r"<(\w+)>\s*<value>([^<]*)</value>", re.IGNORECASE)

#: The status document the newer models still serve. The full-fat
#: `formMainZone_MainZoneXmlStatus.xml` answers 403 on anything recent.
STATUS_PATH = "/goform/formMainZone_MainZoneXmlStatusLite.xml"

#: Where a command goes over HTTP. Denon's own phone app used this endpoint,
#: which is why it survives on models that have dropped the rest of the XML API.
COMMAND_PATH = "/goform/formiPhoneAppDirect.xml"


def _build_commands() -> List[Command]:
    """Everything that does not depend on which inputs were picked."""
    commands = [
        Command(name=name, label=label, description=string, repeatable=repeatable)
        for name, string, label, repeatable in _FIXED
    ]
    commands += [
        Command(
            name="mute",
            label="Mute (toggle)",
            description="Reads whether the receiver is muted and sends the opposite. "
            "Use 'Mute' or 'Unmute' instead if the receiver cannot be read.",
        ),
        Command(
            name="volume",
            label="Set volume",
            description="Sets an absolute level. 0 is silent and 80 is 0 dB, "
            "the number on the receiver's own display plus 80.",
            params={
                "type": "object",
                "required": ["level"],
                "properties": {
                    "level": {"type": "integer", "title": "Level", "minimum": 0, "maximum": 98}
                },
            },
        ),
        Command(
            name="sleep",
            label="Sleep timer",
            description="Switches to standby after this many minutes.",
            params={
                "type": "object",
                "required": ["minutes"],
                "properties": {
                    "minutes": {"type": "integer", "title": "Minutes", "minimum": 1, "maximum": 120}
                },
            },
        ),
    ]
    return commands


COMMANDS: List[Command] = _build_commands()


def build_client(base_url: str, timeout: float) -> httpx.AsyncClient:
    """The one place the HTTP client is made, so the tests have a seam.

    The same role `homeassistant.build_client` plays: a single named function
    the test suite substitutes a `MockTransport` through, rather than reaching
    into the backend to swap a private attribute.
    """
    return httpx.AsyncClient(base_url=base_url, timeout=timeout)


# --------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------


@register
class DenonBackend(Backend, Readable):
    """One Denon or Marantz AV receiver on the local network."""

    name = "denon"
    label = "Denon / Marantz AV receiver"
    description = (
        "A Denon or Marantz network receiver over its local control protocol. No account "
        "and no pairing -- but the receiver's Network Control setting has to be 'Always On'."
    )
    discover_field = "host"

    def __init__(self, device_id: str, config: Dict[str, Any]) -> None:
        super().__init__(device_id, config)
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        self._last_sent = 0.0
        self._health: Optional[Health] = None
        self._health_at = 0.0
        self._state = "stopped"
        self._detail = "not started"

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
                    "router, and set Network Control to 'Always On' on the receiver itself "
                    "or it will vanish from the network in standby.",
                    "default": "",
                },
                "transport": {
                    "type": "string",
                    "title": "How to reach it",
                    "description": "Both carry the same commands. Try HTTP first; switch to "
                    "telnet if the receiver ignores it. Telnet accepts only one program at a "
                    "time, so it may clash with the Denon app or Home Assistant.",
                    "enum": ["http", "telnet"],
                    "default": "http",
                },
                "http_port": {"type": "integer", "title": "HTTP port", "default": DEFAULT_HTTP_PORT},
                "telnet_port": {
                    "type": "integer",
                    "title": "Telnet port",
                    "default": DEFAULT_TELNET_PORT,
                },
                "timeout": {"type": "number", "title": "Timeout (seconds)", "default": DEFAULT_TIMEOUT},
                "entities": {
                    "type": "array",
                    "title": "Inputs",
                    "description": "Which sources this receiver offers commands for. "
                    "Use 'Choose entities' rather than typing them.",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
        }

    @property
    def host(self) -> str:
        return str(self.config.get("host") or "").strip()

    @property
    def transport(self) -> str:
        return "telnet" if str(self.config.get("transport") or "").strip() == "telnet" else "http"

    @property
    def exposed(self) -> List[str]:
        """Input tokens this device offers commands for, in the order they were picked."""
        picked = self.config.get("entities") or []
        return [str(token).strip() for token in picked if str(token).strip()]

    @property
    def _base_url(self) -> str:
        return f"http://{self.host}:{int(self.config.get('http_port') or DEFAULT_HTTP_PORT)}"

    @property
    def _telnet_port(self) -> int:
        return int(self.config.get("telnet_port") or DEFAULT_TELNET_PORT)

    @property
    def _timeout(self) -> float:
        return float(self.config.get("timeout") or DEFAULT_TIMEOUT)

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Builds the transport and probes once.

        Never raises, for the same reason the other two network backends do
        not: a receiver in standby is a normal state rather than a
        configuration error, and one misconfigured device must not stop the
        rest of the hub from starting.
        """
        await self.close()

        if not self.host:
            self._set_state("unconfigured", "no address set")
            return

        if self.transport == "http":
            self._client = build_client(self._base_url, self._timeout)
        await self._probe()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._health = None
        self._health_at = 0.0
        self._set_state("stopped", "not started")

    def _set_state(self, state: str, detail: str) -> None:
        if (state, detail) != (self._state, self._detail):
            logger.info("[denon:%s] %s%s", self.device_id, state, f" -- {detail}" if detail else "")
        self._state = state
        self._detail = detail

    # -- the wire ---------------------------------------------------------

    async def _transmit(self, protocol: str, query: str = "") -> str:
        """Puts one protocol string on the wire, whichever wire that is.

        The lock does double duty: it serialises two buttons pressed at once
        onto a protocol that has no request ids to tell the answers apart, and
        it is what makes the inter-command gap a gap rather than a race.
        """
        async with self._lock:
            gap = MIN_COMMAND_GAP - (time.monotonic() - self._last_sent)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                if self.transport == "telnet":
                    return await self._over_telnet(protocol, query)
                return await self._over_http(protocol)
            finally:
                self._last_sent = time.monotonic()

    async def _over_http(self, protocol: str) -> str:
        client = self._ready()
        # The command is the query string itself, with no `=` after it, so it
        # is built rather than passed as params. Spaces and the `?` of a query
        # need escaping; the slash in `SAT/CBL` is legal as it stands.
        #
        # The escaping is the one thing here taken on trust: whether a given
        # receiver decodes `%20` back into the space in `MSPURE DIRECT` is not
        # something the protocol documents. If a two-word command turns out to
        # be the only one that does nothing, that is where to look -- and the
        # telnet transport, which sends the bytes untouched, is the way out.
        url = f"{COMMAND_PATH}?{quote(protocol, safe='/')}"
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as err:
            self._set_state("error", f"{self.host} refused {protocol}")
            raise BackendError(
                f"{self.device_id}: {protocol} was refused "
                f"({err.response.status_code}) -- try the telnet transport"
            ) from err
        except httpx.HTTPError as err:
            self._set_state("unreachable", f"cannot reach {self.host} ({err})")
            raise BackendError(f"{self.device_id}: {protocol} failed ({err})") from err

        if self._state != "connected":
            self._set_state("connected", "")
        return response.text

    async def _over_telnet(self, protocol: str, query: str = "") -> str:
        """Opens a connection, writes one command, and gets out of the way.

        Per command rather than held open on purpose. The receiver accepts a
        single telnet client, so a socket kept for the evening is a socket the
        Denon app and Home Assistant cannot have.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self._telnet_port), timeout=self._timeout
            )
        except (OSError, asyncio.TimeoutError) as err:
            self._set_state("unreachable", f"cannot reach {self.host} ({_why(err)})")
            raise BackendError(f"{self.device_id}: {protocol} failed ({_why(err)})") from err

        try:
            writer.write(protocol.encode("ascii", "ignore") + b"\r")
            await writer.drain()
            reply = await self._read_reply(reader, query) if query else ""
        except (OSError, asyncio.TimeoutError) as err:
            self._set_state("unreachable", f"cannot reach {self.host} ({_why(err)})")
            raise BackendError(f"{self.device_id}: {protocol} failed ({_why(err)})") from err
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                # The receiver hanging up first is the normal way this ends.
                pass

        if self._state != "connected":
            self._set_state("connected", "")
        return reply

    async def _read_reply(self, reader: asyncio.StreamReader, query: str) -> str:
        """The answer to `query`, skipping past anything else volunteered first."""
        prefix = query.rstrip("?").encode("ascii")
        for _ in range(REPLY_LINES):
            try:
                line = await asyncio.wait_for(reader.readuntil(b"\r"), timeout=REPLY_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                return ""
            answer = line.strip().decode("ascii", "ignore")
            if answer.encode("ascii", "ignore").startswith(prefix):
                return answer[len(prefix) :]
        return ""

    def _ready(self) -> httpx.AsyncClient:
        if self._client is None:
            raise BackendError(f"device '{self.device_id}' is not usable: {self._detail}")
        return self._client

    async def _read_state(self) -> Dict[str, str]:
        """Power, mute and current source, upper-cased, however this transport can.

        Telnet asks three questions and HTTP reads one document, which is the
        one place the two are genuinely different rather than differently
        spelled. A receiver that cannot be reached at all raises; one that
        answers without saying anything this recognises returns the values it
        did manage, which may be none of them. Both callers -- a health line
        and a mute toggle -- have something honest to say either way.
        """
        if self.transport == "telnet":
            return {
                name: (await self._transmit(query, query=query)).upper()
                for name, (query, _key) in READABLE.items()
            }

        body = await self._read_status_document()
        found = {key.lower(): value for key, value in _STATUS_VALUE.findall(body)}
        return {name: found.get(key.lower(), "").upper() for name, (_q, key) in READABLE.items()}

    async def _read_status_document(self) -> str:
        """The status document, if this model still serves one.

        Recent firmware answers 403 here while still taking commands perfectly
        well on the other endpoint, so a refusal is recorded as *reachable*.
        Losing the state display is a smaller thing than reporting a working
        receiver as broken.
        """
        client = self._ready()
        try:
            response = await client.get(STATUS_PATH)
            response.raise_for_status()
        except httpx.HTTPStatusError as err:
            self._set_state("connected", "")
            raise BackendError(
                f"{self.device_id}: this receiver does not report its state "
                f"({err.response.status_code})"
            ) from err
        except httpx.HTTPError as err:
            self._set_state("unreachable", f"cannot reach {self.host} ({err})")
            raise BackendError(f"{self.device_id}: could not read the receiver ({err})") from err
        return response.text

    async def _read_surround(self) -> str:
        """The receiver's resolved surround mode, telnet only -- see `SURROUND_QUERY`.

        This reports what the receiver actually settled on for the current
        signal (`NEURAL:X`, `DOLBY DIGITAL`, `DTS SURROUND`, `STEREO`, ...),
        not necessarily the category last selected (`MSMOVIE`, `MSMUSIC`,
        ...): confirmed against a real unit that sending a category command
        with nothing playing changes nothing this reports, since there is no
        signal for a category to apply to. A condition comparing this against
        a fixed category is only meaningful while audio is actually playing.

        Sending `MS?` over HTTP is harmless -- confirmed empty body, no side
        effect -- but never answers, so `_transmit` is used directly rather
        than gating on `self.transport` first: an empty reply already means
        exactly what it should here, the same way an empty answer to any
        other query would.
        """
        reply = await self._transmit(SURROUND_QUERY, query=SURROUND_QUERY)
        if not reply:
            raise BackendError(f"{self.device_id}: could not read the surround mode")
        return reply.upper()

    # -- commands ---------------------------------------------------------

    async def commands(self) -> List[Command]:
        """The fixed set, plus one per picked input.

        Built from configuration rather than from the receiver, so bindings
        stay editable with the thing switched off at the wall.
        """
        commands = list(COMMANDS)
        for token in self.exposed:
            commands.append(
                Command(
                    name=f"input:{token}",
                    label=f"Input — {INPUT_LABELS.get(token, token)}",
                    description=f"SI{token}",
                )
            )
        return commands

    async def send(self, command: str, params: Optional[Dict[str, Any]] = None) -> None:
        protocol = PROTOCOL.get(command)
        if protocol is not None:
            await self._transmit(protocol)
            return

        if command == "mute":
            await self._toggle_mute()
            return
        if command == "volume":
            await self._transmit(f"MV{_number(command, params, 'level', 0, 98):02d}")
            return
        if command == "sleep":
            await self._transmit(f"SLP{_number(command, params, 'minutes', 1, 120):03d}")
            return

        verb, separator, token = command.partition(":")
        if verb == "input" and separator:
            if token not in self.exposed:
                raise BackendError(
                    f"'{token}' is not an input of device '{self.device_id}' -- "
                    "add it back with 'Choose entities'"
                )
            await self._transmit(f"SI{token}")
            return

        raise BackendError(
            f"device '{self.device_id}' has no command '{command}' "
            "(expected one of its listed commands, or 'input:SOURCE' for a source it exposes)"
        )

    async def _toggle_mute(self) -> None:
        """Mutes or unmutes, whichever the receiver is not already doing.

        The protocol has `MUON` and `MUOFF` but no toggle, and a remote has one
        Mute button. Tracking the state here instead would go wrong the first
        time somebody used the receiver's own remote, so it is read each time.
        """
        try:
            muted = (await self._read_state()).get("mute", "")
        except BackendError as err:
            raise BackendError(f"{err} -- bind 'Mute' or 'Unmute' instead of the toggle") from err
        if not muted:
            raise BackendError(
                f"{self.device_id}: could not read whether the receiver is muted -- "
                "bind 'Mute' or 'Unmute' instead of the toggle"
            )
        await self._transmit("MUOFF" if muted == "ON" else "MUON")

    # -- catalogue --------------------------------------------------------

    async def entities(self) -> List[Dict[str, Any]]:
        """Every source the receiver can select, for the picker.

        The list is fixed by the model rather than read from the unit, so the
        inputs can be chosen with the receiver in standby -- which is when
        somebody setting this up for the first time is most likely to be doing
        it. If it does happen to be awake, the one already selected is marked.
        """
        try:
            current = (await self._read_state()).get("source", "")
        except BackendError:
            current = ""
        return [
            {
                "entity_id": token,
                "name": label,
                "domain": "input",
                "state": "selected" if token.upper() == current else "",
                "controllable": True,
            }
            for token, label in INPUTS
        ]

    # -- health -----------------------------------------------------------

    async def health(self) -> Health:
        """Whether the receiver is answering, and what it is doing if it is."""
        if self._health is not None and time.monotonic() - self._health_at < HEALTH_TTL:
            return self._health
        await self._probe()
        assert self._health is not None
        return self._health

    async def _probe(self) -> None:
        """Asks the receiver how it is, and stamps the health cache with the answer.

        Stamping here rather than in `health()` is what stops the probe
        `connect()` has just done from being repeated by the first poll of the
        device list.
        """
        if not self.host:
            self._set_state("unconfigured", "no address set")
            self._stamp(Health(ok=False, detail=self._detail))
            return

        try:
            state = await self._read_state()
        except BackendError as err:
            state = {}
            logger.debug("[denon:%s] could not read the receiver: %s", self.device_id, err)

        if state.get("power"):
            self._set_state("connected", "")
            self._stamp(Health(ok=True, detail=self._describe(state)))
        elif self._state == "connected":
            # Something answered but would not say what it was doing, which is
            # what a model that has dropped the status document looks like. It
            # still takes commands, so it is healthy -- just quieter.
            self._stamp(Health(ok=True, detail=f"reachable at {self.host}"))
        else:
            self._stamp(Health(ok=False, detail=self._detail or self._state))

    def _stamp(self, health: Health) -> None:
        self._health, self._health_at = health, time.monotonic()

    def _describe(self, state: Dict[str, str]) -> str:
        parts = ["standby" if state.get("power") == "STANDBY" else "on"]
        source = state.get("source", "")
        if source:
            parts.append(INPUT_LABELS.get(source, source))
        if state.get("mute") == "ON":
            parts.append("muted")
        parts.append(f"{len(self.exposed)} input{'' if len(self.exposed) == 1 else 's'}")
        return " · ".join(parts)

    # -- suggestions ------------------------------------------------------

    def suggested_bindings(self) -> Dict[str, str]:
        return dict(SUGGESTED_BINDINGS)

    # -- state --------------------------------------------------------------

    async def readable(self) -> List[StateTarget]:
        """Power, source and mute always -- both transports can answer them.

        Surround is offered only on telnet: advertising it on HTTP would
        mean every read fails, which is honest (`read_state` still raises
        cleanly) but a worse experience than not offering it in the first
        place -- the same reasoning `discover_field`/`pairable` already
        apply per-backend rather than showing a control that can never work.
        """
        targets = [
            StateTarget(target="power", label="Power", values=("on", "standby")),
            StateTarget(target="source", label="Source", values=tuple(INPUT_LABELS)),
            StateTarget(target="muted", label="Muted", values=("true", "false")),
        ]
        if self.transport == "telnet":
            targets.append(
                StateTarget(
                    target="surround",
                    label="Surround mode",
                    description="The resolved processing mode (e.g. DOLBY DIGITAL, NEURAL:X), "
                    "not necessarily the category last selected -- meaningful only while "
                    "something is actually playing. Telnet only.",
                )
            )
        return targets

    async def read_state(self, target: str) -> str:
        """Asks the receiver directly -- unlike `lgtv`/`androidtv` there is no
        live-updating cache here, only whatever `health()`'s own probe last
        saw, which can be stale enough to answer wrongly right after a scene
        just changed it. `BackendError` (unreachable, or nothing answered)
        propagates as-is, for the condition's `on_unreadable` handling.
        """
        if target == "surround":
            return await self._read_surround()
        if target not in ("power", "source", "muted"):
            raise BackendError(f"device '{self.device_id}' has no state '{target}'")
        state = await self._read_state()
        if target == "power":
            return "on" if state.get("power") == "ON" else "standby"
        if target == "muted":
            return "true" if state.get("mute") == "ON" else "false"
        return state.get("source", "")


def _why(err: BaseException) -> str:
    """A readable reason for an exception that may carry no message at all.

    `asyncio.TimeoutError` has never had one, and a bare `ConnectionRefused`
    interpolates as an empty pair of brackets that tells nobody anything.
    """
    return str(err) or type(err).__name__


def _number(command: str, params: Optional[Dict[str, Any]], key: str, low: int, high: int) -> int:
    """One integer parameter, checked before it becomes a protocol string.

    A number outside the range is not merely ignored by the receiver -- the
    wrong digit count shifts every character after it, so `MV1000` is a
    different command rather than a rejected one.
    """
    raw = (params or {}).get(key)
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise BackendError(f"'{command}' needs a '{key}' between {low} and {high}, got {raw!r}")
    if not low <= value <= high:
        raise BackendError(f"'{command}' needs a '{key}' between {low} and {high}, got {value}")
    return value


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

#: Denon announces itself over SSDP rather than the mDNS the other two
#: network backends use. The wire protocol -- the M-SEARCH itself, and
#: reading LOCATION out of the replies -- lives in `_ssdp`, shared with the
#: LG webOS backend; what is Denon-specific is only the search targets and
#: the manufacturer filter below.

#: The two of Denon's three published device types that something worth
#: adding to the hub answers to. `MediaServer` is left out: nothing answers
#: only to it that does not also answer to one of these.
_SEARCH_TARGETS = (
    "urn:schemas-denon-com:device:AiosDevice:1",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
)

#: Matched case-insensitively as a substring of the description's
#: `<manufacturer>`, which covers Denon's four published spellings --
#: including "DENON PROFESSIONAL" -- without listing them out. A HEOS speaker
#: or soundbar matches this too and is listed anyway: telling those apart
#: from an AVR needs a probe of the status endpoint, and recent firmware
#: answers that one with 403 while taking commands perfectly well, so a
#: stricter filter would risk hiding a working receiver to screen out a
#: speaker the picker costs one glance to skip.
_MANUFACTURERS = ("denon", "marantz")

_DESC_FIELD = _ssdp.DESC_FIELD
_msearch = _ssdp.msearch


async def _ssdp_search(timeout: float) -> List[str]:
    """Every Denon/Marantz `LOCATION` URL that answered an M-SEARCH.

    Its own function, the same role `build_client` plays for the HTTP
    transport above: a seam a test can substitute so discovery is exercised
    without a real network.
    """
    return await _ssdp.search(timeout, _SEARCH_TARGETS)


async def discover(timeout: float = 3.0) -> List[Dict[str, Any]]:
    """Denon and Marantz receivers announcing themselves over SSDP.

    Same job and the same failure mode as the Android TV and Home Assistant
    discovery in their own backends: nothing answering returns an empty list
    rather than raising, because the address field is still there to type
    into. A receiver in standby with `Network Control` at its factory default
    is off the network entirely and will not appear here -- the same thing
    that makes it unreachable once its address has been typed in by hand.
    """
    try:
        locations = await _ssdp_search(timeout)
    except OSError as err:  # pragma: no cover - no usable network interface
        logger.warning("Could not start discovery: %s", err)
        return []

    found: Dict[str, Dict[str, Any]] = {}
    try:
        # `build_client` again -- the same seam the per-device transport
        # uses. An empty base does nothing here, since every request below
        # is an absolute URL from a `LOCATION` header; it exists so a test
        # can substitute a `MockTransport` the same way it does for the rest
        # of this file.
        async with build_client("", timeout) as client:
            for location in locations:
                host = urlparse(location).hostname
                if not host or host in found:
                    continue
                try:
                    response = await client.get(location)
                    response.raise_for_status()
                except httpx.HTTPError as err:
                    logger.debug("Denon discovery: %s did not answer (%s)", location, err)
                    continue
                fields = {name.lower(): value for name, value in _DESC_FIELD.findall(response.text)}
                manufacturer = fields.get("manufacturer", "").lower()
                if not any(known in manufacturer for known in _MANUFACTURERS):
                    continue
                name = fields.get("friendlyname") or fields.get("modelname") or host
                found[host] = {
                    "name": name,
                    "host": host,
                    "version": fields.get("modelname", ""),
                }
    except Exception as err:  # pragma: no cover - defensive
        logger.warning("Discovery failed: %s", err)

    return sorted(found.values(), key=lambda entry: entry["name"])
