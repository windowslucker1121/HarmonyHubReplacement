"""Home Assistant, over its REST API.

Home Assistant is the odd one out among the backends: every other one talks
to a single box that does a fixed number of things, so its command list is
written once in Python. A Home Assistant install has hundreds of entities and
they are different in every house, so the command list has to be built from
whatever this particular instance happens to have.

That difference drives the two decisions that shape this file.

**Commands are per-entity, and the entities are chosen.** `config["entities"]`
names the handful worth putting on a remote; nothing else is offered. Listing
every entity would produce thousands of dropdown rows, and the alternative --
one `turn_on` command with an entity id typed in as a parameter -- is exactly
the "typo fails silently at press time" failure the `Command` interface exists
to prevent. Picking from a live list makes a wrong entity id impossible rather
than merely late. The same idea as the `http` backend declaring its requests,
except the declaration is generated rather than typed.

A command is `verb:entity_id` -- `toggle:light.kitchen`, `brighter:light.sofa`,
`activate:scene.movie_night`. Colons cannot occur in a verb, so the split is
unambiguous, and the pair reads correctly in a binding and in the live log.
Anything the verb tables cannot express is a declared action in
`config["actions"]`, which is a raw service call with whatever data it needs.

**The token is not configuration.** A long-lived access token is unrestricted
-- it can do anything its user can -- and `GET /api/config` is readable by
anything on the LAN. So it lives in `credentials/` next to the Android TV
certificates, and gets there through the `Pairable` conversation rather than
through the device form. Nothing here ever logs it or hands it back.

Everything is REST. There is no socket to hold open, so unlike the Android TV
backend there is no reconnect loop: a request either works or it does not, and
`health()` re-probes on a short timer, which means a Home Assistant that was
down when the hub started heals itself as soon as anyone looks at the device
list. `turn_on` and `turn_off` are idempotent in Home Assistant -- unlike the
Shield's POWER, which is a toggle -- so none of this needs to read state
first. `mute` is the one exception, and says why at the point it does it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from . import Backend, BackendError, Command, FocusTarget, Health, Pairable, register

logger = logging.getLogger("HUB.homeassistant")

#: Where the access token goes, following the same convention as the Android
#: TV certificates and for the same reason: it is secret and instance-specific.
DEFAULT_CREDENTIALS_DIR = "credentials"

DEFAULT_TIMEOUT = 5.0

#: How long a health result is reused. `GET /api/state` polls the device list,
#: so without this every poll would put two requests on the Home Assistant box.
HEALTH_TTL = 10.0

#: mDNS service Home Assistant advertises itself under.
SERVICE_TYPE = "_home-assistant._tcp.local."

#: Domains that report rather than obey. Kept as a denylist, not an allowlist:
#: Home Assistant gains domains with every release, and an unknown one that
#: turns out to be controllable is a better failure than a new light platform
#: silently missing from the picker.
READ_ONLY_DOMAINS = frozenset(
    {
        "air_quality", "binary_sensor", "calendar", "device_tracker", "event",
        "geo_location", "image", "person", "sensor", "stt", "sun", "tag",
        "tts", "update", "weather", "zone",
    }
)


# --------------------------------------------------------------------------
# Verbs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Verb:
    """One thing that can be done to an entity of a given domain."""

    name: str
    label: str
    #: Fully qualified service, e.g. `light.turn_on`.
    service: str
    #: Extra service data sent with the call.
    data: Dict[str, Any] = field(default_factory=dict)
    repeatable: bool = False
    description: str = ""
    #: Set for a verb that has no toggle service of its own. Names a state
    #: attribute to read and invert -- see `mute` below.
    toggle_attribute: Optional[str] = None
    #: "up" or "down" for a verb the remote's SmartHome +/- keys can step
    #: through -- see `HomeAssistantBackend.adjust_command`. Most verbs are
    #: not steppable and leave this `None`.
    adjust: Optional[str] = None
    #: Brightness (0-100) to turn a light on at when stepping it "up" from
    #: off. `brightness_step_pct` has nothing to step from an off light, so
    #: stepping up from off means switching it on instead of doing nothing.
    turn_on_pct: Optional[int] = None


def _v(name, label, service, data=None, repeatable=False, description="",
       toggle_attribute=None, adjust=None, turn_on_pct=None):
    return Verb(name, label, service, data or {}, repeatable, description,
                toggle_attribute, adjust, turn_on_pct)


#: How many steps from off to fully bright a "brighter" press on an off
#: light should count as. Five is the same coarse granularity as the remote's
#: other steppers -- fine enough to feel gradual, coarse enough that turning
#: a dark room's light on does not open at a blinding 100%.
BRIGHTNESS_TURN_ON_STEPS = 5
BRIGHTNESS_TURN_ON_PCT = 100 // BRIGHTNESS_TURN_ON_STEPS


# `homeassistant.turn_on` and friends work on any domain that supports being
# switched, which keeps the common case to one row per verb rather than one
# per domain.
_ON_OFF: Tuple[Verb, ...] = (
    _v("turn_on", "On", "homeassistant.turn_on"),
    _v("turn_off", "Off", "homeassistant.turn_off"),
    _v("toggle", "Toggle", "homeassistant.toggle"),
)

VERBS: Dict[str, Tuple[Verb, ...]] = {
    "light": _ON_OFF
    + (
        # Relative, so holding the key ramps without reading the current
        # brightness first. `brighter` is the one exception: it still checks
        # whether the light is off, because a step has nothing to step from
        # there, and turns it on at `BRIGHTNESS_TURN_ON_PCT` instead.
        _v("brighter", "Brighter", "light.turn_on", {"brightness_step_pct": 10}, True,
           "Steps brightness up 10%, or turns the light on if it was off. Safe to hold.",
           adjust="up", turn_on_pct=BRIGHTNESS_TURN_ON_PCT),
        _v("dimmer", "Dimmer", "light.turn_on", {"brightness_step_pct": -10}, True,
           "Steps brightness down 10%. Safe to hold.", adjust="down"),
    ),
    "switch": _ON_OFF,
    "input_boolean": _ON_OFF,
    "fan": _ON_OFF
    + (
        _v("increase_speed", "Speed up", "fan.increase_speed", repeatable=True,
           description="Steps fan speed up one increment. Safe to hold.", adjust="up"),
        _v("decrease_speed", "Speed down", "fan.decrease_speed", repeatable=True,
           description="Steps fan speed down one increment. Safe to hold.", adjust="down"),
    ),
    "humidifier": _ON_OFF,
    "siren": _ON_OFF,
    "climate": (
        _v("turn_on", "On", "climate.turn_on"),
        _v("turn_off", "Off", "climate.turn_off"),
    ),
    "scene": (
        _v("activate", "Activate", "scene.turn_on", description="Applies this Home Assistant scene."),
    ),
    "script": (
        _v("run", "Run", "script.turn_on"),
        _v("stop", "Stop", "script.turn_off"),
    ),
    "automation": (
        _v("trigger", "Trigger", "automation.trigger"),
        _v("turn_on", "Enable", "automation.turn_on"),
        _v("turn_off", "Disable", "automation.turn_off"),
    ),
    "button": (_v("press", "Press", "button.press"),),
    "input_button": (_v("press", "Press", "input_button.press"),),
    "lock": (
        _v("lock", "Lock", "lock.lock"),
        _v("unlock", "Unlock", "lock.unlock"),
    ),
    "cover": (
        _v("open", "Open", "cover.open_cover"),
        _v("close", "Close", "cover.close_cover"),
        _v("stop", "Stop", "cover.stop_cover"),
        _v("toggle", "Toggle", "cover.toggle"),
    ),
    "media_player": (
        _v("play_pause", "Play / pause", "media_player.media_play_pause"),
        _v("next", "Next", "media_player.media_next_track"),
        _v("previous", "Previous", "media_player.media_previous_track"),
        _v("volume_up", "Volume up", "media_player.volume_up", repeatable=True, adjust="up"),
        _v("volume_down", "Volume down", "media_player.volume_down", repeatable=True, adjust="down"),
        # Home Assistant has no toggle for this: `volume_mute` is told which
        # way to go. A remote's mute key is a toggle, so this one verb reads
        # the entity first -- the same reasoning that makes the Shield's
        # `power_on` check before sending POWER.
        _v("mute", "Mute", "media_player.volume_mute", toggle_attribute="is_volume_muted"),
        _v("turn_on", "On", "media_player.turn_on"),
        _v("turn_off", "Off", "media_player.turn_off"),
    ),
    "vacuum": (
        _v("start", "Start", "vacuum.start"),
        _v("pause", "Pause", "vacuum.pause"),
        _v("return_home", "Return to dock", "vacuum.return_to_base"),
    ),
    "remote": _ON_OFF,
    "water_heater": (
        _v("turn_on", "On", "water_heater.turn_on"),
        _v("turn_off", "Off", "water_heater.turn_off"),
    ),
}

#: Anything not named above still switches, which covers most of what Home
#: Assistant will grow next.
DEFAULT_VERBS: Tuple[Verb, ...] = _ON_OFF

#: Domains whose name belongs in the label. "Movie Night" is ambiguous in a
#: dropdown that also lists hub scenes; "Scene: Movie Night" is not.
_LABEL_PREFIX = {"scene": "Scene: ", "script": "Script: ", "automation": "Automation: "}


def verbs_for(entity_id: str) -> Tuple[Verb, ...]:
    return VERBS.get(entity_id.split(".", 1)[0], DEFAULT_VERBS)


def _titled(entity_id: str) -> str:
    """A readable name for an entity Home Assistant has not described yet."""
    return entity_id.split(".", 1)[-1].replace("_", " ").title()


def build_client(url: str, token: str, timeout: float, verify: bool) -> httpx.AsyncClient:
    """The one place an HTTP client is made, for the two that need one.

    `connect()` builds the long-lived one and `pair_finish()` builds a
    throwaway to check a token before keeping it, and they must agree on
    every setting or a token could verify against one configuration and be
    used against another. Being a single named function also gives the tests
    somewhere to substitute a transport, the same way the Android TV suite
    substitutes its client.
    """
    return httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        verify=verify,
    )


# --------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------


@register
class HomeAssistantBackend(Backend, Pairable):
    """One Home Assistant instance, exposing the entities picked from it."""

    name = "homeassistant"
    label = "Home Assistant"
    description = (
        "Lights, switches, scenes and scripts from a Home Assistant instance. Pick the "
        "entities worth putting on the remote and each one becomes a set of commands."
    )
    discover_field = "url"

    # Copy for the pairing conversation. Android TV shows a code on the
    # television; here the user is fetching a token out of a web page, which
    # is the same two-step shape and completely different words.
    pair_label = "Connect to Home Assistant"
    pair_hint = "Home Assistant issues a token instead of showing a code. Only needed once."
    pair_input_label = "Long-lived access token"
    pair_input_multiline = True

    def __init__(self, device_id: str, config: Dict[str, Any]) -> None:
        super().__init__(device_id, config)
        self._client: Optional[httpx.AsyncClient] = None
        self._state = "stopped"
        self._detail = "not started"
        #: entity_id -> friendly name, refreshed whenever a probe succeeds.
        #: Only ever a nicety: `commands()` works from configuration alone, so
        #: bindings can be edited with Home Assistant switched off.
        self._names: Dict[str, str] = {}
        #: Whether `_names` is an answer or an absence. Emptiness cannot tell
        #: the two apart, and confusing them would report every exposed entity
        #: as deleted the moment one request failed.
        self._names_read = False
        self._version = ""
        self._location = ""
        self._health: Optional[Health] = None
        self._health_at = 0.0

    # -- configuration ----------------------------------------------------

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {
                    "type": "string",
                    "title": "Address",
                    "description": "Where Home Assistant is served, e.g. "
                    "http://homeassistant.local:8123",
                    "default": "",
                },
                "verify_tls": {
                    "type": "boolean",
                    "title": "Verify TLS certificates",
                    "description": "Turn off only for an https instance with a "
                    "self-signed certificate.",
                    "default": True,
                },
                "timeout": {"type": "number", "title": "Timeout (seconds)", "default": DEFAULT_TIMEOUT},
                "entities": {
                    "type": "array",
                    "title": "Exposed entities",
                    "description": "Which entities this device offers commands for. "
                    "Use 'Choose entities' rather than typing them.",
                    "items": {"type": "string"},
                    "default": [],
                },
                "actions": {
                    "type": "array",
                    "title": "Custom actions",
                    "description": "Raw service calls, for anything the per-entity "
                    "commands cannot express.",
                    "items": {
                        "type": "object",
                        "required": ["name", "service"],
                        "properties": {
                            "name": {"type": "string"},
                            "label": {"type": "string"},
                            "service": {"type": "string"},
                            "target": {"type": "object"},
                            "data": {"type": "object"},
                        },
                    },
                    "default": [],
                },
                "token_file": {
                    "type": "string",
                    "title": "Token file",
                    "description": "Where the access token is kept. Blank means "
                    f"'{DEFAULT_CREDENTIALS_DIR}'. The token itself is never stored "
                    "in this configuration.",
                    "default": "",
                },
            },
        }

    @property
    def url(self) -> str:
        return str(self.config.get("url") or "").strip().rstrip("/")

    @property
    def exposed(self) -> List[str]:
        """Entity ids this device offers commands for, in the order they were picked."""
        entities = self.config.get("entities") or []
        return [str(e).strip() for e in entities if str(e).strip()]

    @property
    def _declared(self) -> Dict[str, Dict[str, Any]]:
        """Custom actions, keyed by name.

        A name containing a colon is dropped rather than registered: it could
        never be told apart from a `verb:entity_id` command, and silently
        shadowing one would be worse than not existing.
        """
        actions: Dict[str, Dict[str, Any]] = {}
        for spec in self.config.get("actions") or []:
            name = str(spec.get("name") or "").strip()
            if not name:
                continue
            if ":" in name:
                logger.warning(
                    "[homeassistant:%s] ignoring custom action '%s': a name cannot contain ':'",
                    self.device_id, name,
                )
                continue
            actions[name] = spec
        return actions

    def _token_path(self) -> Path:
        directory = Path(str(self.config.get("token_file") or "").strip() or DEFAULT_CREDENTIALS_DIR)
        return directory / f"homeassistant_{self.device_id}.token"

    def _read_token(self) -> str:
        try:
            return self._token_path().read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _write_token(self, token: str) -> None:
        path = self._token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token.strip() + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Windows and most network shares do not implement this. The file
            # is inside a gitignored directory either way; tightening it is a
            # bonus, not something worth failing pairing over.
            pass

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Builds the client and probes once.

        Never raises, for the same reason the Android TV backend does not: the
        engine only registers a backend whose `connect()` returned, and an
        unregistered backend cannot be reached by the pairing routes -- so a
        device with no token yet could never be given one.
        """
        await self.close()

        if not self.url:
            self._set_state("unconfigured", "no address set")
            return

        token = self._read_token()
        if not token:
            self._set_state("unpaired", "no access token -- connect this device to use it")
            return

        self._client = build_client(self.url, token, self._timeout, self._verify)
        await self._probe()

    @property
    def _timeout(self) -> float:
        return float(self.config.get("timeout") or DEFAULT_TIMEOUT)

    @property
    def _verify(self) -> bool:
        return bool(self.config.get("verify_tls", True))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._health = None
        self._health_at = 0.0
        self._names_read = False
        self._set_state("stopped", "not started")

    def _set_state(self, state: str, detail: str) -> None:
        if (state, detail) != (self._state, self._detail):
            logger.info(
                "[homeassistant:%s] %s%s", self.device_id, state, f" -- {detail}" if detail else ""
            )
        self._state = state
        self._detail = detail

    async def _probe(self) -> None:
        """Asks the instance how it is, and stamps the health cache with the answer.

        Stamping here rather than in `health()` is what stops the probe
        `connect()` has just done from being immediately repeated by the
        first poll of the device list.
        """
        await self._probe_once()
        self._stamp(
            self._describe()
            if self._state == "connected"
            else Health(ok=False, detail=self._detail or self._state)
        )

    def _stamp(self, health: Health) -> None:
        self._health, self._health_at = health, time.monotonic()

    async def _probe_once(self) -> None:
        """One round trip that both authenticates and describes the instance.

        `/api/config` is the useful probe rather than `/api/`: it answers 401
        for a bad token exactly the same way, and when it succeeds it carries
        the version and the house's name, which is what makes the device list
        entry recognisable as *your* Home Assistant.
        """
        if self._client is None:
            return
        try:
            response = await self._client.get("/api/config")
            if response.status_code == 401:
                self._set_state("unpaired", "the token was rejected -- create a new one")
                return
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as err:
            self._set_state("unreachable", f"cannot reach {self.url} ({err})")
            return
        except ValueError as err:
            self._set_state("error", f"{self.url} did not answer with JSON ({err})")
            return

        self._version = str(body.get("version") or "")
        self._location = str(body.get("location_name") or "")
        self._set_state("connected", "")
        await self._refresh_names()

    async def _refresh_names(self) -> None:
        """Caches friendly names, so commands read as they do in Home Assistant.

        A failure here is deliberately quiet: the names are cosmetic, and
        `commands()` falls back to the entity id. Losing them must not make a
        working instance look broken.
        """
        if self._client is None:
            return
        try:
            response = await self._client.get("/api/states")
            response.raise_for_status()
            states = response.json()
        except (httpx.HTTPError, ValueError) as err:
            logger.debug("[homeassistant:%s] could not list states: %s", self.device_id, err)
            return
        self._names = {
            state["entity_id"]: str((state.get("attributes") or {}).get("friendly_name") or "")
            or _titled(state["entity_id"])
            for state in states
            if isinstance(state, dict) and state.get("entity_id")
        }
        self._names_read = True

    # -- commands ---------------------------------------------------------

    async def commands(self) -> List[Command]:
        """Every exposed entity expanded by its domain, plus the declared actions.

        Built from configuration, not from Home Assistant, so bindings can be
        edited while the instance is unreachable. Names are decorated with
        whatever the last successful probe cached.
        """
        commands: List[Command] = []
        for entity_id in self.exposed:
            domain = entity_id.split(".", 1)[0]
            name = self._names.get(entity_id) or _titled(entity_id)
            prefix = _LABEL_PREFIX.get(domain, "")
            for verb in verbs_for(entity_id):
                commands.append(
                    Command(
                        name=f"{verb.name}:{entity_id}",
                        label=f"{prefix}{name} — {verb.label}",
                        description=verb.description or f"{verb.service} on {entity_id}",
                        repeatable=verb.repeatable,
                    )
                )

        for action_name, spec in self._declared.items():
            commands.append(
                Command(
                    name=action_name,
                    label=str(spec.get("label") or action_name.replace("_", " ").title()),
                    description=str(spec.get("service") or ""),
                )
            )
        return commands

    def suggested_bindings(self) -> Dict[str, str]:
        """The remote's six SmartHome keys, pointed at what was exposed.

        These are the keys the Android TV backend deliberately leaves alone --
        two bulbs, two sockets, and a pair of +/- keys next to them. That is
        this backend's job almost exactly, so the suggestion is a real one
        rather than a token gesture: the two bulb keys toggle the first two
        lights picked, the two socket keys the first two switches. The +/-
        keys are not assigned here -- see `suggested_adjust`, since what they
        step is decided at press time by whatever was touched last, not by a
        fixed command chosen now.
        """
        lights = [e for e in self.exposed if e.startswith("light.")]
        sockets = [e for e in self.exposed if e.split(".", 1)[0] in ("switch", "input_boolean")]

        suggested: Dict[str, str] = {}
        if lights:
            suggested["consumer_0x0ff2"] = f"toggle:{lights[0]}"
        if len(lights) > 1:
            suggested["consumer_0x0ff3"] = f"toggle:{lights[1]}"
        if sockets:
            suggested["consumer_0x0ff4"] = f"toggle:{sockets[0]}"
        if len(sockets) > 1:
            suggested["consumer_0x0ff5"] = f"toggle:{sockets[1]}"
        return suggested

    def suggested_adjust(self) -> Dict[str, str]:
        """The remote's +/- keys, left to follow whatever was touched last.

        Offered only once something exposed can actually be stepped -- an
        instance with nothing adjustable exposed leaves these two keys free
        for `suggested_bindings` or the binding editor to use for something
        else instead of suggesting a key that would only ever report "nothing
        to turn up".
        """
        has_adjustable = any(any(v.adjust for v in verbs_for(e)) for e in self.exposed)
        if not has_adjustable:
            return {}
        return {"consumer_0x0ff0": "up", "consumer_0x0ff1": "down"}

    def focus_for(self, command: str) -> Optional[FocusTarget]:
        """The entity `command` acted on, for the +/- keys to remember.

        Any recognised `verb:entity_id` command takes the focus, adjustable
        or not -- toggling a switch is as much "the last thing touched" as
        dimming a light, and pressing + right after is meant to say "nothing
        to turn up here" rather than silently reaching past it to an older
        light. A custom action or a malformed command takes nothing, since
        there is no entity to remember.
        """
        _, separator, entity_id = command.partition(":")
        if not separator or not entity_id or entity_id not in self.exposed:
            return None
        name = self._names.get(entity_id) or _titled(entity_id)
        return FocusTarget(target=entity_id, label=name)

    def adjust_command(self, target: str, direction: str) -> Optional[str]:
        """The command that steps `target` `up` or `down`, if it has one."""
        if target not in self.exposed:
            return None
        verb = next((v for v in verbs_for(target) if v.adjust == direction), None)
        return f"{verb.name}:{target}" if verb else None

    async def send(self, command: str, params: Optional[Dict[str, Any]] = None) -> None:
        declared = self._declared.get(command)
        if declared is not None:
            service = str(declared.get("service") or "")
            if "." not in service:
                raise BackendError(
                    f"custom action '{command}' needs a service like 'light.turn_on', got '{service}'"
                )
            data = dict(declared.get("data") or {})
            data.update(declared.get("target") or {})
            await self._call_service(service, data)
            return

        verb_name, separator, entity_id = command.partition(":")
        if not separator or not entity_id:
            raise BackendError(
                f"device '{self.device_id}' has no command '{command}' "
                "(expected 'verb:entity_id', or the name of a custom action)"
            )
        if entity_id not in self.exposed:
            # Reachable when an entity is removed from the picker but a
            # binding still points at it. Failing loudly here is the whole
            # reason exposure is explicit.
            raise BackendError(
                f"'{entity_id}' is not exposed by device '{self.device_id}' -- "
                "add it back with 'Choose entities'"
            )

        verb = next((v for v in verbs_for(entity_id) if v.name == verb_name), None)
        if verb is None:
            offered = ", ".join(v.name for v in verbs_for(entity_id))
            raise BackendError(f"'{entity_id}' has no '{verb_name}' command (offers: {offered})")

        data: Dict[str, Any] = {"entity_id": entity_id, **verb.data}
        if verb.toggle_attribute is not None:
            current = await self._attribute(entity_id, verb.toggle_attribute)
            data[verb.toggle_attribute] = not bool(current)
        if verb.turn_on_pct is not None and await self._entity_state(entity_id) == "off":
            # A step has nothing to step from on an off light -- turn it on
            # at a fixed starting level instead of sending a step that Home
            # Assistant would otherwise have to guess a base for.
            data = {"entity_id": entity_id, "brightness_pct": verb.turn_on_pct}
        await self._call_service(verb.service, data)

    async def _attribute(self, entity_id: str, attribute: str) -> Any:
        """One attribute of one entity, for the verbs that have to invert it."""
        client = self._ready()
        try:
            response = await client.get(f"/api/states/{entity_id}")
            response.raise_for_status()
            return (response.json().get("attributes") or {}).get(attribute)
        except (httpx.HTTPError, ValueError) as err:
            raise BackendError(f"could not read {entity_id} ({err})") from err

    async def _entity_state(self, entity_id: str) -> str:
        """The entity's own `state` string (`"on"`, `"off"`, ...), not an attribute."""
        client = self._ready()
        try:
            response = await client.get(f"/api/states/{entity_id}")
            response.raise_for_status()
            return str(response.json().get("state") or "")
        except (httpx.HTTPError, ValueError) as err:
            raise BackendError(f"could not read {entity_id} ({err})") from err

    def _ready(self) -> httpx.AsyncClient:
        if self._client is None:
            raise BackendError(f"device '{self.device_id}' is not usable: {self._detail}")
        return self._client

    async def _call_service(self, service: str, data: Dict[str, Any]) -> None:
        """Posts one service call.

        Targets go in the body rather than in a `target` block: that is what
        the REST endpoint takes, and `entity_id`, `area_id` and `device_id`
        are all ordinary fields in it.
        """
        client = self._ready()
        domain, _, service_name = service.partition(".")
        try:
            response = await client.post(f"/api/services/{domain}/{service_name}", json=data)
            if response.status_code == 401:
                self._set_state("unpaired", "the token was rejected -- create a new one")
                raise BackendError(f"{self.device_id}: Home Assistant rejected the token")
            response.raise_for_status()
        except httpx.HTTPStatusError as err:
            raise BackendError(
                f"{self.device_id}: {service} failed "
                f"({err.response.status_code} {err.response.text[:200]})"
            ) from err
        except httpx.HTTPError as err:
            raise BackendError(f"{self.device_id}: {service} failed ({err})") from err
        # A call that reached Home Assistant means the instance is up, whatever
        # the last probe concluded.
        if self._state != "connected":
            self._set_state("connected", "")

    # -- catalogue --------------------------------------------------------

    async def entities(self) -> List[Dict[str, Any]]:
        """Everything this instance has, for the picker.

        Read-only domains are flagged rather than dropped, so the picker can
        default to hiding them without this deciding for it -- and so a domain
        nobody here has heard of still shows up.
        """
        client = self._ready()
        try:
            response = await client.get("/api/states")
            response.raise_for_status()
            states = response.json()
        except (httpx.HTTPError, ValueError) as err:
            raise BackendError(f"could not list entities ({err})") from err

        found = []
        for state in states:
            if not isinstance(state, dict) or not state.get("entity_id"):
                continue
            entity_id = state["entity_id"]
            domain = entity_id.split(".", 1)[0]
            found.append(
                {
                    "entity_id": entity_id,
                    "name": str((state.get("attributes") or {}).get("friendly_name") or "")
                    or _titled(entity_id),
                    "domain": domain,
                    "state": str(state.get("state") or ""),
                    "controllable": domain not in READ_ONLY_DOMAINS,
                }
            )
        return sorted(found, key=lambda entry: (entry["domain"], entry["name"].lower()))

    # -- health -----------------------------------------------------------

    async def health(self) -> Health:
        """Whether this instance is reachable, and whether the config still fits it.

        The second half matters as much as the first. Renaming an entity in
        Home Assistant leaves every binding pointing at it broken, and nothing
        else in the system would notice until the button was pressed.
        """
        if self._health is not None and time.monotonic() - self._health_at < HEALTH_TTL:
            return self._health

        if self._client is None:
            self._stamp(Health(ok=False, detail=self._detail or self._state))
        else:
            await self._probe()
        assert self._health is not None
        return self._health

    def _describe(self) -> Health:
        parts = [p for p in (self._location, f"HA {self._version}" if self._version else "") if p]

        # Only claim entities are gone when there is genuinely something to
        # have compared them against. A state list that could not be read is
        # an absence, not an empty instance, and treating the two alike would
        # report every binding as broken the moment one request timed out.
        missing = [e for e in self.exposed if e not in self._names] if self._names_read else []
        if missing:
            shown = ", ".join(missing[:3]) + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
            return Health(ok=False, detail=" · ".join(parts + [f"missing from Home Assistant: {shown}"]))

        parts.append(f"{len(self.exposed)} entit{'y' if len(self.exposed) == 1 else 'ies'}")
        return Health(ok=True, detail=" · ".join(parts))

    # -- pairing ----------------------------------------------------------

    async def pair_start(self) -> str:
        if not self.url:
            raise BackendError(f"device '{self.device_id}' has no address set")
        return (
            f"In Home Assistant open your profile at {self.url}/profile/security, scroll to "
            "'Long-lived access tokens', choose 'Create token', and paste it here. "
            "Home Assistant shows it once."
        )

    async def pair_finish(self, code: str) -> None:
        """Checks the token works before keeping it.

        Writing first and finding out later would leave a device that looks
        connected and fails on the first press, and the token is shown by
        Home Assistant exactly once -- so a rejected one has to be reported
        while the user still has it on screen.
        """
        token = (code or "").strip()
        if not token:
            raise BackendError("no token given")
        if not self.url:
            raise BackendError(f"device '{self.device_id}' has no address set")

        async with build_client(self.url, token, self._timeout, self._verify) as client:
            try:
                response = await client.get("/api/config")
            except httpx.HTTPError as err:
                raise BackendError(f"could not reach {self.url} ({err})") from err
            if response.status_code == 401:
                self._set_state("unpaired", "the token was rejected")
                raise BackendError("Home Assistant did not accept that token")
            if response.status_code >= 400:
                raise BackendError(
                    f"{self.url} answered {response.status_code} -- is that a Home Assistant?"
                )

        try:
            self._write_token(token)
        except OSError as err:
            raise BackendError(f"could not save the token to {self._token_path()}: {err}") from err

        await self.connect()
        if self._state != "connected":
            raise BackendError(f"token accepted, but could not connect: {self._detail}")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


async def discover(timeout: float = 3.0) -> List[Dict[str, Any]]:
    """Home Assistant instances announcing themselves on the local network.

    Mirrors the Android TV discovery, including its failure mode: a network
    where mDNS does not work returns nothing rather than raising, because the
    address field is still there to type into.
    """
    try:
        from zeroconf import ServiceStateChange
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
            properties = {
                (key.decode(errors="replace") if isinstance(key, bytes) else str(key)): (
                    value.decode(errors="replace") if isinstance(value, bytes) else str(value or "")
                )
                for key, value in (info.properties or {}).items()
                if key
            }
            # Home Assistant publishes the URL it believes it is reachable at,
            # which is worth more than an address guessed from the A record:
            # it carries the right scheme and port.
            url = properties.get("internal_url") or properties.get("base_url") or ""
            if not url:
                addresses = info.parsed_addresses()
                if not addresses:
                    continue
                url = f"http://{addresses[0]}:{info.port or 8123}"
            found.append(
                {
                    "name": properties.get("location_name")
                    or name.removesuffix("." + SERVICE_TYPE)
                    or name,
                    "host": url,
                    "version": properties.get("version", ""),
                }
            )
    except Exception as err:  # pragma: no cover - defensive
        logger.warning("Discovery failed: %s", err)
    finally:
        await browser.async_cancel()
        await azc.async_close()

    return sorted(found, key=lambda entry: entry["name"])
