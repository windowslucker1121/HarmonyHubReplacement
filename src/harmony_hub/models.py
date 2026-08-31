"""The configuration domain model: devices, scenes, bindings, actions.

Everything here is plain data with no behaviour and no I/O, so the whole
configuration can be validated, round-tripped through JSON, and diffed in
tests without a radio, a network, or a running engine.

The shape follows the Harmony Hub's own model, which got two things right
that are worth copying rather than reinventing:

* A scene is not a macro, it is a *context*. While it is active it decides
  what every button means, so "Volume Up" can reach the AV receiver in one
  scene and a soundbar in another.
* Devices have a power *policy* rather than being blindly switched, so
  moving between two scenes that share a TV does not power-cycle the TV.
  `SceneEngine` reads it when a scene stops -- see `_stop_actions` in
  `engine.py` -- to decide which of the outgoing scene's power-offs to skip.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Dict, Iterator, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONFIG_VERSION = 1


class Base(BaseModel):
    """Rejects unknown keys so a typo in hand-edited config is an error, not a silent no-op."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


class DeviceAction(Base):
    """Send one command to one configured device."""

    type: Literal["device"] = "device"
    device: str
    command: str
    params: Dict[str, Any] = Field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.device}.{self.command}"


class SceneAction(Base):
    """Switch to a scene, or stop the active one when `scene` is null.

    This is what makes the remote's own activity buttons work: they bind to
    a scene action like any other button, rather than scene switching being
    a special case wired into the engine.
    """

    type: Literal["scene"] = "scene"
    scene: Optional[str] = None

    def describe(self) -> str:
        return f"scene:{self.scene}" if self.scene else "scene:off"


class DelayAction(Base):
    """Wait before the next action.

    Real hardware needs this: a TV that has just been powered on will ignore
    an input-select command sent a few milliseconds later.
    """

    type: Literal["delay"] = "delay"
    seconds: float = Field(gt=0, le=60)

    def describe(self) -> str:
        return f"delay {self.seconds}s"


class AdjustAction(Base):
    """Step whatever device was touched last, up or down.

    This is what the remote's SmartHome +/- keys run: rather than naming a
    device and command the way `DeviceAction` does, it asks the engine for
    whichever device most recently claimed the "focus" -- the light just
    toggled, the speaker just muted -- and asks that device's backend for
    the command that steps it in `direction`. Which device that is changes
    from press to press; nothing here can name it in advance.

    `device` and `target` are a fallback for when nothing has been touched
    yet -- right after the hub restarts, the focus is empty, and without a
    fallback these keys would do nothing at all. `target` is backend-private
    (an entity id, for Home Assistant), so it only makes sense alongside the
    `device` that produced it; one without the other is a configuration
    mistake rather than something the engine could resolve.
    """

    type: Literal["adjust"] = "adjust"
    direction: Literal["up", "down"]
    device: Optional[str] = None
    target: Optional[str] = None

    @model_validator(mode="after")
    def _check_fallback(self) -> "AdjustAction":
        if self.target is not None and self.device is None:
            raise ValueError("an adjust action's 'target' means nothing without 'device'")
        return self

    def describe(self) -> str:
        arrow = "up" if self.direction == "up" else "down"
        if self.device and self.target:
            return f"adjust {arrow}: {self.device}.{self.target} (or whatever was touched last)"
        return f"adjust {arrow}: whatever was touched last"


# --------------------------------------------------------------------------
# Values & conditions
# --------------------------------------------------------------------------


class StateValue(Base):
    """A device's current state, read live when this value is needed.

    The `device`/`target` pair is exactly what `Backend.read_state` takes --
    `target` is backend-private the same way a command name is, chosen from
    whatever the device's own `readable()` offers.
    """

    type: Literal["state"] = "state"
    device: str
    target: str

    def describe(self) -> str:
        return f"{self.device}.{self.target}"


class VarValue(Base):
    """A value a `SetAction` stored earlier, recalled by name.

    Unset until some `set` action for `name` has actually run -- there is no
    config-time default, since the entire point is capturing something the
    engine could not know until a scene was under way. Reading one that was
    never set behaves as `SceneEngine.variables` documents: the same as an
    unreadable device state.
    """

    type: Literal["var"] = "var"
    name: str = Field(pattern=r"^[a-z0-9_]+$")

    def describe(self) -> str:
        return f"${self.name}"


class LiteralValue(Base):
    """A value fixed in configuration -- what an ordinary condition compared
    against before `Value` existed, expressed as the same type as the other
    two so a condition's two sides, or a `set` action's source, are never a
    special case depending on which kind of value they happen to be.
    """

    type: Literal["literal"] = "literal"
    value: str = ""

    def describe(self) -> str:
        return f"'{self.value}'"


Value = Annotated[Union[StateValue, VarValue, LiteralValue], Field(discriminator="type")]

ConditionOp = Literal["is", "is_not", "contains", "in", "gt", "lt", "known", "unknown"]


class Condition(Base):
    """One thing to check before running a branch of actions.

    `known` and `unknown` ask whether `left` could be read at all, rather
    than comparing it to anything -- `right` must be left unset for either.
    Every other operator compares `left` against `right` as plain strings;
    `gt` and `lt` additionally try to parse both as numbers, and behave as
    `on_unreadable` describes if either does not parse.

    `on_unreadable` decides what happens when `left` cannot be read at all --
    the device is offline, is not a `Readable` backend, or (for a `var`
    value) was never set. `"run"`, the default, matches the engine's
    behaviour before conditions existed: nothing here ever stops a macro
    just because a read failed. `"skip"` is for the opposite case, where
    acting on a guess is worse than doing nothing -- never power off a
    device this cannot confirm is actually on.
    """

    left: Value
    op: ConditionOp = "is"
    right: Optional[Value] = None
    on_unreadable: Literal["run", "skip"] = "run"

    @model_validator(mode="after")
    def _check_operands(self) -> "Condition":
        needs_right = self.op not in ("known", "unknown")
        if needs_right and self.right is None:
            raise ValueError(f"condition '{self.op}' needs a value to compare against")
        if not needs_right and self.right is not None:
            raise ValueError(f"condition '{self.op}' does not compare against anything")
        return self

    def describe(self) -> str:
        if self.op in ("known", "unknown"):
            return f"{self.left.describe()} is {self.op}"
        return f"{self.left.describe()} {self.op} {self.right.describe()}"


class IfAction(Base):
    """Runs `then` if `condition` holds, `otherwise` if it does not.

    The general form every simpler case reduces to: a single action gated on
    a condition is just a one-action `then` with an empty `otherwise`, and
    "turn the TV on, wait, then pick the input -- but only if it was off" is
    the block written exactly as it reads. `otherwise` rather than `else`,
    which Dart reserves -- both branches nest to `MAX_ACTION_DEPTH`, the same
    bound scene-switching actions already share.
    """

    type: Literal["if"] = "if"
    condition: Condition
    then: List["Action"] = Field(default_factory=list)
    otherwise: List["Action"] = Field(default_factory=list)

    def describe(self) -> str:
        return f"if {self.condition.describe()}"


class SetAction(Base):
    """Remembers `value` under `name`, for a later `var` value to recall.

    The store is hub-lifetime, not scoped to the scene that set it or to one
    press -- see `SceneEngine.variables`. Not scoping it to the scene is
    deliberate: "the input the TV was on before this scene changed it" is
    exactly as useful read back from a different scene's stop macro as from
    this one's own.
    """

    type: Literal["set"] = "set"
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    value: Value

    def describe(self) -> str:
        return f"set {self.name} = {self.value.describe()}"


class WaitForAction(Base):
    """Polls `condition` until it holds, or `timeout` runs out.

    The honest replacement for a fixed `DelayAction` used as a guess: "wait 2
    seconds and hope the TV is on by then" becomes "wait until the TV
    reports on, for up to `timeout` seconds". `poll` is how often to check in
    between, kept separate from `timeout` so a device that answers instantly
    is not hammered every 100ms while a slow one is waited on.

    `on_timeout` decides how a timeout is reported, not whether the macro
    keeps going -- nothing in this engine lets one action's failure cancel
    its siblings, `run_actions` least of all. `"continue"` (the default)
    logs it as an ordinary, successful step; `"stop"` raises instead, so it
    is logged as a failure the way an unreachable device would be -- for the
    step that genuinely went wrong rather than merely being skippable, even
    though whatever comes after it in the macro still runs either way.
    """

    type: Literal["wait_for"] = "wait_for"
    condition: Condition
    timeout: float = Field(default=10.0, gt=0, le=120)
    poll: float = Field(default=0.5, gt=0, le=10)
    on_timeout: Literal["continue", "stop"] = "continue"

    def describe(self) -> str:
        return f"wait for {self.condition.describe()}"


Action = Annotated[
    Union[DeviceAction, SceneAction, DelayAction, AdjustAction, IfAction, SetAction, WaitForAction],
    Field(discriminator="type"),
]

# `IfAction.then`/`otherwise` refer to `Action` before it exists -- rebuilt
# now that it does, so the forward reference resolves deterministically at
# import time rather than on whatever request happens to validate one first.
IfAction.model_rebuild()


# --------------------------------------------------------------------------
# Bindings
# --------------------------------------------------------------------------


class Binding(Base):
    """What one button does, split by the phase of the press.

    `on_repeat` exists because the remote emits a packet roughly every 100ms
    while a button is held: volume should ramp, but power must not fire
    repeatedly.

    `repeat_delay` and `repeat_interval` are ordinary auto-repeat, the same
    two knobs a keyboard has, and they matter more than they look. Without a
    delay every one of those 100ms packets fires the repeat actions, so an
    ordinary 300ms press -- which is what "a short press" measures at --
    sends the command three or four times. Waiting before repeating is what
    separates "held" from "pressed", and no packet can tell you which one it
    was at the moment it arrives.

    Left unset (the common case), a binding follows `HubConfig`'s
    `default_repeat_delay` / `default_repeat_interval` rather than carrying
    its own copy of a value that is almost always the same for every button
    on the remote. Setting one here is the override for the one button --
    usually something slow like a blind or a projector lens -- that
    genuinely needs different timing from everything else.

    `repeat_accel` and `repeat_accel_seconds` ramp the interval down further
    the longer the button stays held, up to `repeat_accel` times the base
    rate once `repeat_accel_seconds` has elapsed. `repeat_accel` at its
    default of `1.0` means no ramp -- repeats fire at the flat rate
    `repeat_interval` already describes, exactly as before this existed.
    Like the two settings above, `None` follows `HubConfig`'s
    `default_repeat_accel` / `default_repeat_accel_seconds`.

    `on_hold` changes the timing of `on_press`. When a hold action is
    configured there is no way to know whether a press is short or long until
    the button is either released or held long enough, so `on_press` is held
    back until then. Buttons without a hold action stay immediate -- paying
    that latency everywhere to support the few buttons that need it would
    make the whole remote feel sluggish.
    """

    on_press: List[Action] = Field(default_factory=list)
    on_repeat: List[Action] = Field(default_factory=list)
    on_hold: List[Action] = Field(default_factory=list)
    on_release: List[Action] = Field(default_factory=list)
    hold_seconds: float = Field(default=0.6, gt=0, le=10)

    #: Overrides `HubConfig.default_repeat_delay` for this button alone.
    #: `None` (the default) follows the config-wide setting.
    repeat_delay: Optional[float] = Field(default=None, ge=0, le=10)

    #: Overrides `HubConfig.default_repeat_interval` for this button alone.
    #: `None` (the default) follows the config-wide setting.
    repeat_interval: Optional[float] = Field(default=None, ge=0, le=10)

    #: Overrides `HubConfig.default_repeat_accel` for this button alone.
    #: `None` (the default) follows the config-wide setting.
    repeat_accel: Optional[float] = Field(default=None, ge=1, le=16)

    #: Overrides `HubConfig.default_repeat_accel_seconds` for this button
    #: alone. `None` (the default) follows the config-wide setting.
    repeat_accel_seconds: Optional[float] = Field(default=None, gt=0, le=30)

    @property
    def is_empty(self) -> bool:
        return not (self.on_press or self.on_repeat or self.on_hold or self.on_release)

    def actions_for(self, phase: str) -> List[Action]:
        return getattr(self, f"on_{phase}", [])


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


class PowerPolicy(str, Enum):
    """How aggressively the engine may power a device on and off.

    Mirrors the three choices a Harmony Hub offers, which exist because the
    right answer genuinely differs per device: a TV should follow the scene,
    an AV receiver is often left on across scenes, and a device on a smart
    plug or one with no discrete power command must never be guessed at.
    """

    MANAGED = "managed"  # on when a scene needs it, off when no scene does
    LEAVE_ON = "leave_on"  # on when first needed, off only on an explicit stop
    MANUAL = "manual"  # never send power commands


class Device(Base):
    """A configured instance of a backend -- "the living room TV, over IR".

    `config` is backend-specific and validated by the backend itself rather
    than here, so adding a backend never means touching this model.
    """

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    backend: str
    config: Dict[str, Any] = Field(default_factory=dict)
    power_policy: PowerPolicy = PowerPolicy.MANAGED
    power_on_command: Optional[str] = None
    power_off_command: Optional[str] = None

    def power_on_name(self) -> str:
        """The command name that turns this device on, override or the usual default."""
        return self.power_on_command or "power_on"

    def power_off_name(self) -> str:
        """The command name that turns this device off, override or the usual default."""
        return self.power_off_command or "power_off"

    def is_power_off(self, command: str) -> bool:
        return command == self.power_off_name()

    def is_power_on(self, command: str) -> bool:
        return command == self.power_on_name()


# --------------------------------------------------------------------------
# Scenes
# --------------------------------------------------------------------------


class Scene(Base):
    """A named context: which devices take part, and what the buttons do."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    icon: Optional[str] = None
    devices: List[str] = Field(default_factory=list)
    on_start: List[Action] = Field(default_factory=list)
    on_stop: List[Action] = Field(default_factory=list)
    bindings: Dict[str, Binding] = Field(default_factory=dict)

    def actions(self):
        """Every action this scene can run, macros and bindings alike, paired
        with where it came from -- the same traversal `HubConfig._all_actions`
        needs for validation and the engine needs to work out which devices a
        scene actually touches when `devices` itself is left empty.

        An `IfAction`'s `then`/`otherwise` are walked too, so a `DeviceAction`
        buried inside a condition is validated and counted towards
        `required_devices()` exactly as one sitting at the top level would be
        -- neither of those should care that a branch happens to be
        conditional. The two branches share their parent's `where` label
        rather than growing their own: precise enough to find the binding a
        validation error came from, without a label that grows one clause
        longer per nesting level.
        """
        def walk(actions: List[Action], where: str) -> Iterator["tuple[str, Action]"]:
            for action in actions:
                yield where, action
                if isinstance(action, IfAction):
                    yield from walk(action.then, where)
                    yield from walk(action.otherwise, where)

        for label, actions in (("on_start", self.on_start), ("on_stop", self.on_stop)):
            yield from walk(actions, f"scene '{self.id}'.{label}")
        for key, binding in self.bindings.items():
            for phase in ("press", "repeat", "hold", "release"):
                yield from walk(binding.actions_for(phase), f"scene '{self.id}' binding '{key}'.on_{phase}")

    def required_devices(self) -> "set[str]":
        """Which devices this scene depends on.

        The declared `devices` list wins when set, so unticking a device in
        the scene editor is a real instruction -- "this scene no longer needs
        it" -- rather than something the engine second-guesses. A scene that
        leaves `devices` empty (hand-written config, or one built through the
        API) falls back to whatever its own actions actually touch, so it is
        never treated as needing nothing.
        """
        if self.devices:
            return set(self.devices)
        return {
            action.device
            for _, action in self.actions()
            if isinstance(action, (DeviceAction, AdjustAction)) and action.device
        }


def _value_device(value: Optional[Value]) -> Optional[str]:
    """The device a `state` value reads from, or `None` for a `var`/`literal`
    value -- which name nothing that `HubConfig._check_references` could
    validate.
    """
    return value.device if isinstance(value, StateValue) else None


def _condition_devices(condition: Condition) -> Iterator[str]:
    for value in (condition.left, condition.right):
        device = _value_device(value)
        if device is not None:
            yield device


def _raw_state_device(raw: Any) -> Optional[str]:
    """The device id, if `raw` is a `state` value written as a plain dict
    inside a `DeviceAction`'s `params` -- e.g. `set_input`'s `source`
    restoring whatever another device last reported. `params` stays untyped
    (`Dict[str, Any]`) rather than a schema that would have to describe
    every command's own parameters twice; `SceneEngine._resolve_param`
    recognises the same shape at the point a macro actually runs.
    """
    if isinstance(raw, dict) and raw.get("type") == "state":
        device = raw.get("device")
        return str(device) if device else None
    return None


# --------------------------------------------------------------------------
# Root
# --------------------------------------------------------------------------


class HubConfig(Base):
    """The whole configuration: one file, one atomic save.

    `global_scene`, if set, names one of `scenes` whose bindings are the
    fallback for buttons the active scene does not bind, and the only
    bindings in effect when no scene is running. It is a reference rather
    than a second, parallel set of bindings to maintain: the remote's
    activity buttons still need somewhere to work from the idle state, but
    that somewhere is an ordinary scene -- one that can be renamed, deleted,
    or bound to like any other -- rather than configuration that only
    half-behaves like one.

    `default_repeat_delay` and `default_repeat_interval` are the same idea
    applied to auto-repeat: one setting for the whole remote instead of a
    copy on every binding that repeats, because in practice they are all the
    same number. A `Binding` can still set its own and override this for the
    one button that genuinely needs different timing.

    `default_repeat_accel` and `default_repeat_accel_seconds` layer an
    exponential ramp on top of that: the longer a button stays held, the
    faster its repeats fire, up to `default_repeat_accel` times the base
    rate once `default_repeat_accel_seconds` of holding has passed.
    `default_repeat_accel` at `1.0` (the default) disables the ramp entirely
    -- repeats stay at the flat rate the two settings above already produce.
    """

    version: int = CONFIG_VERSION
    devices: List[Device] = Field(default_factory=list)
    scenes: List[Scene] = Field(default_factory=list)
    global_scene: Optional[str] = None
    default_scene: Optional[str] = None

    default_repeat_delay: float = Field(default=0.5, ge=0, le=10)
    default_repeat_interval: float = Field(default=0.0, ge=0, le=10)
    default_repeat_accel: float = Field(default=1.0, ge=1, le=16)
    default_repeat_accel_seconds: float = Field(default=3.0, gt=0, le=30)

    def device(self, device_id: str) -> Optional[Device]:
        return next((d for d in self.devices if d.id == device_id), None)

    def scene(self, scene_id: str) -> Optional[Scene]:
        return next((s for s in self.scenes if s.id == scene_id), None)

    def _all_actions(self):
        """Every action anywhere in the config, paired with where it came from."""
        for scene in self.scenes:
            yield from scene.actions()

    @model_validator(mode="after")
    def _check_references(self) -> "HubConfig":
        """Catches dangling references at load time rather than mid-press.

        A scene that points at a deleted device would otherwise fail only
        when someone happens to press the button that uses it, which is the
        worst possible moment to discover a typo.
        """
        device_ids = {d.id for d in self.devices}
        scene_ids = {s.id for s in self.scenes}

        duplicates = len(device_ids) != len(self.devices) or len(scene_ids) != len(self.scenes)
        if duplicates:
            raise ValueError("device and scene ids must each be unique")

        problems = []
        for scene in self.scenes:
            for device_id in scene.devices:
                if device_id not in device_ids:
                    problems.append(f"scene '{scene.id}' lists unknown device '{device_id}'")

        for where, action in self._all_actions():
            if isinstance(action, DeviceAction):
                if action.device not in device_ids:
                    problems.append(f"{where} targets unknown device '{action.device}'")
                for param_name, raw in action.params.items():
                    ref = _raw_state_device(raw)
                    if ref is not None and ref not in device_ids:
                        problems.append(f"{where} param '{param_name}' reads unknown device '{ref}'")
            elif isinstance(action, SceneAction) and action.scene and action.scene not in scene_ids:
                problems.append(f"{where} targets unknown scene '{action.scene}'")
            elif isinstance(action, AdjustAction) and action.device and action.device not in device_ids:
                problems.append(f"{where} falls back to unknown device '{action.device}'")
            elif isinstance(action, (IfAction, WaitForAction)):
                for ref in _condition_devices(action.condition):
                    if ref not in device_ids:
                        problems.append(f"{where} condition reads unknown device '{ref}'")
            elif isinstance(action, SetAction):
                ref = _value_device(action.value)
                if ref is not None and ref not in device_ids:
                    problems.append(f"{where} sets from unknown device '{ref}'")

        if self.default_scene and self.default_scene not in scene_ids:
            problems.append(f"default_scene '{self.default_scene}' does not exist")

        if self.global_scene and self.global_scene not in scene_ids:
            problems.append(f"global_scene '{self.global_scene}' does not exist")

        if problems:
            raise ValueError("; ".join(problems))
        return self
