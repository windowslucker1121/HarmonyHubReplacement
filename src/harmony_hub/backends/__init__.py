"""Backend plugin interface and registry.

A *backend* is a way of talking to equipment -- infrared, Android TV, an
HTTP endpoint. The engine knows nothing about any of them: it only ever
calls `send()` on something that satisfies this interface.

Two things here exist specifically so the configuration UI can be built
without hard-coding knowledge of each backend:

* `config_schema()` returns JSON Schema for a device's settings, so the
  "add a device" form is generated rather than written per backend.
* `commands()` returns what this device can actually do, so binding a button
  is a dropdown rather than a free-text field where typos fail silently at
  press time.

Backends shipped with the project register with `@register`. Third-party
backends are found through the `harmony_hub.backends` entry point group, so
a separate package can add one without any change to this project.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Literal, Optional, Type

if TYPE_CHECKING:  # pragma: no cover
    # `LearnStatus` genuinely lives in `harmony_hub.ir.learn` -- it is IR's
    # own state, not a backend concept -- and is only ever needed here as a
    # type. Importing it for real would make this package depend on `ir`,
    # which then depends back on `Learnable` from here; keeping it to a
    # type-only import (safe under `from __future__ import annotations`,
    # already active above) avoids that cycle entirely.
    from ..ir.learn import LearnStatus

logger = logging.getLogger("HUB.backends")

ENTRY_POINT_GROUP = "harmony_hub.backends"


@dataclass(frozen=True)
class Command:
    """One thing a device can be told to do."""

    name: str
    label: str
    # JSON Schema for this command's parameters. Empty means it takes none,
    # which is the common case: most remote-control commands are just verbs.
    params: Dict[str, Any] = field(default_factory=dict)
    description: str = ""
    #: Whether firing this repeatedly while a button is held is sensible.
    #: Volume should ramp; power must not toggle forty times a second.
    repeatable: bool = False


@dataclass(frozen=True)
class Health:
    """Whether a device is reachable, and something readable if it is not."""

    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class StateTarget:
    """One thing a device can report the state of, for a condition to read.

    Mirrors `Command`: `target` is what a condition's config actually stores
    (backend-private, the same way a command name or `FocusTarget.target`
    is), `label` and `values` are what the editor shows around it. `values`
    lists the states this target is known to take -- `["on", "off"]" for a
    power state -- which is what lets the condition editor offer a dropdown
    instead of a free-text field; empty means the value space is open (a
    volume level, an input name) and free text is the only option anyway.
    """

    target: str
    label: str
    values: tuple = ()
    description: str = ""


@dataclass(frozen=True)
class FocusTarget:
    """What a command acted on, so a later relative command can find it again.

    This is how the remote's SmartHome +/- keys know what to step: every
    successful `DeviceAction` is offered to `Backend.focus_for`, and a
    backend that recognises the command as having acted on something
    returns one of these. The engine remembers it without knowing what it
    means -- `target` is backend-private, the same way a command name is.
    """

    #: Backend-private identifier, e.g. a Home Assistant entity id.
    target: str
    #: Human-readable, for the log and the live view.
    label: str = ""


class BackendError(RuntimeError):
    """A backend could not carry out a request."""


class Backend(ABC):
    """Base class for every way of controlling equipment.

    Instances are per-configured-device, not per-backend-type, so a backend
    may hold a connection, a session, or a socket for its own device.

    Implementations must be safe to call from an asyncio event loop. Anything
    genuinely blocking belongs in a thread (`asyncio.to_thread`), because
    stalling the loop would also stall the remote's event stream and the web
    UI along with it.
    """

    #: Stable identifier used in configuration files. Must be unique.
    name: ClassVar[str]
    #: Shown in the UI's backend picker.
    label: ClassVar[str] = ""
    #: One line describing what this backend talks to.
    description: ClassVar[str] = ""
    #: Which config field a `discover()` result fills in, e.g. "host" or
    #: "url" -- empty if this backend's module has no `discover()`. Declared
    #: here rather than inferred from `config_schema()` because more than one
    #: field could plausibly hold an address; the app reads this rather than
    #: keeping its own list of which backend names are discoverable, the same
    #: way it already reads `pairable` instead of hard-coding backend names.
    discover_field: ClassVar[str] = ""

    def __init__(self, device_id: str, config: Dict[str, Any]) -> None:
        self.device_id = device_id
        self.config = config

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        """JSON Schema for this backend's device settings. Drives the device form."""
        return {"type": "object", "properties": {}}

    async def connect(self) -> None:
        """Prepare to send commands. Called once at startup and after config changes."""

    async def close(self) -> None:
        """Release anything `connect()` acquired."""

    @abstractmethod
    async def commands(self) -> List[Command]:
        """Everything this device can be asked to do. Drives the binding editor."""

    @abstractmethod
    async def send(self, command: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Carry out one command. Raise `BackendError` if it could not be done."""

    async def health(self) -> Health:
        """Whether this device is currently reachable."""
        return Health(ok=True)

    def suggested_bindings(self) -> Dict[str, str]:
        """A starting point for mapping the remote at this device.

        Maps a button key from `buttons.json` to one of this device's command
        names. A backend that knows what a normal remote looks like can fill
        this in and save the user forty-eight trips through the binding
        editor; the default is empty, and nothing depends on it.
        """
        return {}

    def suggested_adjust(self) -> Dict[str, str]:
        """A starting point for the remote's +/- keys: button key to direction.

        Separate from `suggested_bindings` because its value is not a command
        name -- an adjust binding steps whatever the engine's focus points at,
        which is decided at press time, not now. A backend with nothing that
        can be stepped offers none, and the two bulb/-socket suggestion keys
        stay free for `suggested_bindings` to fill with something else.
        """
        return {}

    def focus_for(self, command: str) -> Optional[FocusTarget]:
        """What `command` acted on, if anything a later +/- press could step.

        Called after every command this backend successfully sends, so it
        must be synchronous and do no I/O. Returning `None` (the default)
        means this backend never takes the focus -- which is why pressing
        Volume Up on the Shield does not steal the SmartHome keys away from
        whatever light was last touched.
        """
        return None

    def adjust_command(self, target: str, direction: Literal["up", "down"]) -> Optional[str]:
        """The command that steps `target` `up` or `down`, if one exists.

        `target` is whatever this same backend previously handed back from
        `focus_for` -- never another backend's. Returning `None` (the
        default) means this target cannot be stepped that way, which the
        engine reports rather than sending nothing at all.
        """
        return None


class Pairable(ABC):
    """Mixin for a backend that needs a one-time handshake before it works.

    Some equipment will not accept commands from an unknown client until a
    human has confirmed it -- Android TV puts a code on screen, and other
    protocols do something equivalent. That is a conversation, not a setting,
    so it cannot live in `config_schema()`; it gets its own two API routes
    instead, which any backend adopting this mixin gets for free.

    Whatever a pairing leaves behind (a certificate, a token) is the
    backend's business, and belongs outside the configuration file: pairing
    should not have to round-trip through the device form.

    The four `pair_*` class attributes are the words the UI puts around that
    conversation. They exist because the mechanism generalises but the
    wording does not: an Android TV shows a six-digit code on the screen, and
    a Home Assistant issues a long token from a web page. Hard-coding either
    one would make the pairing screen lie to everyone using the other.

    Not every handshake has something to type back, either -- an LG webOS TV
    just wants a press on its own remote. `pair_input_label = ""` is that
    case: the caller skips the text field entirely and calls `pair_finish`
    with an empty string once the hint's instruction has been carried out,
    rather than asking for input a backend has nothing to do with.
    """

    #: Wording for the pairing screen. The defaults suit a device that shows
    #: a code; a backend whose handshake looks different overrides them.
    pair_label: ClassVar[str] = "Pair this device"
    pair_hint: ClassVar[str] = (
        "The device puts a code on its screen; typing it back here is the whole setup. "
        "Only needed once."
    )
    #: Empty means there is nothing to type back -- see the class docstring.
    pair_input_label: ClassVar[str] = "Code"
    #: Whether the value is long enough to want a multi-line box. Also decides
    #: whether the field accepts anything other than digits.
    pair_input_multiline: ClassVar[bool] = False

    @abstractmethod
    async def pair_start(self) -> str:
        """Begins pairing. Returns what to tell the user to do next."""

    @abstractmethod
    async def pair_finish(self, code: str) -> None:
        """Completes pairing with the code the user read off the device."""


class Learnable(ABC):
    """Mixin for a backend that can learn its own commands from a remote.

    Distinct from `Pairable`: pairing is a one-time handshake with the
    *device being controlled*; this is a repeated capture from a *different*
    remote, aimed at whatever IR receiver the install is wired to. A backend
    can be either, both, or neither.

    A single learn job asks for the same button twice and requires the two
    captures to agree before reporting `"captured"` -- a partial or noisy
    single capture is the single most common way IR learning goes wrong, and
    a second press that disagrees is the cheapest way to catch it before it
    is saved as a command that will not play back correctly. A `"mismatch"`
    is not a dead end: `learn_start` can simply be called again for the same
    name, which is what the app does with a "try again" button rather than
    surfacing it as an error state.

    There is exactly one IR receiver per install (`HubSettings.ir_rx_pin`),
    shared by every `Learnable` device configured against it, so only one
    learn job can be in flight at a time across *all* of them -- a second
    device's `learn_start` while one is already running is refused, the same
    way `DiscoveryJob` refuses a second search for the one radio.

    The `learn_*` class attributes exist for the same reason `Pairable`'s
    `pair_*` ones do: the mechanism generalises, the wording does not. A
    receive-only install still learns fine but cannot verify a capture by
    playing it back, which is what `learn_verifiable` is for.
    """

    learn_label: ClassVar[str] = "Learn a command"
    learn_hint: ClassVar[str] = (
        "Point the remote at the receiver and press the button. You'll be asked to "
        "press it a second time, to confirm the capture."
    )
    #: Whether a capture can be replayed through the transmitter to check it
    #: before it is saved. False for a receive-only install -- see the class
    #: docstring -- where there is nothing to play it back through.
    learn_verifiable: ClassVar[bool] = True

    @abstractmethod
    async def learn_start(self, timeout: float) -> LearnStatus:
        """Begins listening for one command. Poll `learn_status` for progress."""

    @abstractmethod
    def learn_status(self) -> LearnStatus:
        """Where the in-progress (or most recently finished) learn has got to."""

    @abstractmethod
    def learn_cancel(self) -> LearnStatus:
        """Asks an in-progress learn to stop."""

    @abstractmethod
    async def learn_verify(self) -> None:
        """Replays the most recent captured-and-confirmed timings.

        Lets a code be checked against the real equipment before it is named
        and saved. Raises `BackendError` if there is nothing captured yet, or
        if `learn_verifiable` is false.
        """

    @abstractmethod
    async def learn_save(
        self, name: str, label: str, *, repeatable: bool = False, repeats: int = 1
    ) -> None:
        """Names the most recent captured-and-confirmed timings and keeps them.

        Learning the same `name` again is a re-teach, not an error -- see
        `ir.codes.CodeSet.add`.
        """

    @abstractmethod
    async def learn_forget(self, name: str) -> None:
        """Removes a previously learned command."""


class Readable(ABC):
    """Mixin for a backend that can report a device's current state back.

    This is what a scene's `if` and `wait_for` actions read from: "is the TV
    already on", "what input is it showing". Distinct from `commands()` --
    which is one-directional, a verb the engine can send -- because a
    condition needs a noun to compare against, not something to do.

    A backend that never implements this (infrared, an HTTP webhook, a
    fire-and-forget shell command) genuinely has nothing to answer here --
    there is no return channel -- so it is left off entirely rather than
    given a `read_state` that always raises. The engine and the editor both
    check `isinstance(backend, Readable)` before offering conditions on a
    device, the same way `Pairable` and `Learnable` are checked today.
    """

    @abstractmethod
    async def readable(self) -> List["StateTarget"]:
        """Everything this device can report the state of. Drives the condition editor."""

    @abstractmethod
    async def read_state(self, target: str) -> str:
        """The current value of `target`, as a plain string for a condition to compare.

        Raise `BackendError` if the value cannot be read right now (the
        device is unreachable, `target` does not exist) -- the engine turns
        that into the condition's own `on_unreadable` handling rather than
        letting it abort the macro.
        """


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_REGISTRY: Dict[str, Type[Backend]] = {}
_discovered = False


def register(cls: Type[Backend]) -> Type[Backend]:
    """Registers a backend class. Usable as a decorator."""
    if not getattr(cls, "name", None):
        raise ValueError(f"{cls.__name__} must define a `name`")
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        raise ValueError(f"backend name '{cls.name}' is already registered")
    _REGISTRY[cls.name] = cls
    return cls


def discover() -> None:
    """Loads third-party backends advertised through the entry point group.

    A backend that fails to import is logged and skipped rather than being
    allowed to take the whole hub down: one broken plugin should not stop
    the remote from working with everything else.
    """
    global _discovered
    if _discovered:
        return
    _discovered = True

    from importlib.metadata import entry_points

    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        try:
            register(entry_point.load())
            logger.info("Loaded backend plugin '%s'", entry_point.name)
        except Exception:
            logger.exception("Backend plugin '%s' failed to load; skipping", entry_point.name)


def available() -> Dict[str, Type[Backend]]:
    """Every registered backend class, keyed by name."""
    discover()
    return dict(_REGISTRY)


def get(name: str) -> Type[Backend]:
    """Looks up a backend class by name."""
    backends = available()
    if name not in backends:
        known = ", ".join(sorted(backends)) or "none"
        raise KeyError(f"unknown backend '{name}' (registered: {known})")
    return backends[name]


def create(name: str, device_id: str, config: Dict[str, Any]) -> Backend:
    """Instantiates a backend for one configured device."""
    return get(name)(device_id, config)


# Importing the built-ins is what runs their @register decorators.
from . import androidtv as _androidtv  # noqa: E402,F401
from . import denon as _denon  # noqa: E402,F401
from . import homeassistant as _homeassistant  # noqa: E402,F401
from . import http as _http  # noqa: E402,F401
from . import ir as _ir  # noqa: E402,F401
from . import lgtv as _lgtv  # noqa: E402,F401
from . import shell as _shell  # noqa: E402,F401
from . import virtual as _virtual  # noqa: E402,F401

__all__ = [
    "Backend",
    "BackendError",
    "Command",
    "FocusTarget",
    "Health",
    "Learnable",
    "Pairable",
    "Readable",
    "StateTarget",
    "available",
    "create",
    "discover",
    "get",
    "register",
]
