"""The scene engine: turns remote button events into device commands.

This is the only place that knows how a press becomes an action. It holds
the active scene, the live backend instances, and the timing state needed to
tell a tap from a hold.

It deliberately knows nothing about *how* any device is reached. Every
outbound call goes through the `Backend` interface, so adding infrared or
Android TV support later changes nothing in this file.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from harmony_receiver.events import RemoteEvent
from harmony_receiver.profiles import ButtonMap

from . import backends
from .events import EventBroker, HubEvent
from .models import (
    Action,
    AdjustAction,
    Binding,
    DelayAction,
    DeviceAction,
    HubConfig,
    PowerPolicy,
    Scene,
    SceneAction,
)

logger = logging.getLogger("HUB.engine")

# How far a chain of scene-switching actions may nest before the engine
# assumes the configuration loops. Scenes triggering scenes is legitimate
# (an "everything off" scene stopping another), but a cycle would otherwise
# spin forever.
MAX_ACTION_DEPTH = 5

# An action that has not finished by now is not going to; a wedged backend
# must not hold up the rest of the macro or the next button press.
ACTION_TIMEOUT = 15.0

# Hard ceiling on how many times one repeat packet may fire the repeat
# actions, however high `repeat_accel` climbs. Not a setting -- a backstop
# against a misconfigured ramp flooding a backend.
MAX_REPEAT_BURST = 8

# The most real time a single repeat packet may be credited with, whether
# for the acceleration ramp or the fractional-repeat accumulator. Bounds how
# big a catch-up burst a backend stall (or a queue backlog) can produce once
# it clears -- without this, a `now - last` grown large while nothing was
# actually being processed would otherwise be spent all at once the moment
# processing resumes.
MAX_REPEAT_DT = 0.5


@dataclass(frozen=True)
class Focus:
    """What the remote's SmartHome +/- keys currently follow.

    Set by the engine, not chosen by a binding: after every successful
    `DeviceAction` the owning backend is asked what it just touched
    (`Backend.focus_for`), and if it names something, that becomes the
    focus. No binding ever names a device and target pair directly -- an
    `AdjustAction` always resolves through this instead, which is what lets
    the same two keys follow whichever light or speaker was touched last.
    """

    device: str
    target: str
    label: str


class SceneEngine:
    """Routes remote events to actions, according to the active scene.

    Button lookup falls back from the active scene to whichever scene
    `config.global_scene` names -- a reference, not a second set of bindings
    to keep in sync. That fallback is what lets volume and power keep working
    in every scene without being copied into each one, and it is the only
    thing in effect when no scene is running -- which is exactly the state
    the remote's activity buttons have to work from.
    """

    def __init__(self, config: HubConfig, buttons: ButtonMap, broker: Optional[EventBroker] = None) -> None:
        self.config = config
        self.buttons = buttons
        self.broker = broker or EventBroker()
        self.active_scene: Optional[str] = None

        #: While true, `handle()` still publishes every button event -- the
        #: live log keeps working -- but never dispatches to an action. For
        #: trying a real remote (or a replay) against real hardware without
        #: any of it actually reaching a device: useful on a Pi wired up for
        #: testing, where a stray press must not be indistinguishable from a
        #: real command. Not persisted and not settings -- like
        #: `active_scene`, it resets to unpaused on every hub (re)start,
        #: since a fresh `SceneEngine` is what a restart creates.
        self.paused: bool = False

        #: What the SmartHome +/- keys currently follow. `None` before
        #: anything has been touched, or after `stop()`.
        self._focus: Optional[Focus] = None

        self._backends: Dict[str, backends.Backend] = {}
        # Buttons whose press is being held back while we wait to see if it
        # turns into a hold. Keyed by button; the bool records whether the
        # hold already fired, so release knows not to also send the tap.
        self._pending: Dict[str, asyncio.Task] = {}
        self._hold_fired: Dict[str, bool] = {}

        # Auto-repeat bookkeeping: when the current press began, and when its
        # last repeat fired. Both are cleared on release.
        self._press_at: Dict[str, float] = {}
        self._repeat_at: Dict[str, float] = {}

        # Fractional repeats saved up between packets once acceleration has
        # pushed the rate past one repeat per packet, and how long the ramp
        # itself has been running -- see `_repeats_due`. Both cleared
        # alongside the two dicts above.
        self._credits: Dict[str, float] = {}
        self._ramp_elapsed: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Brings up a backend instance for every configured device."""
        await self._build_backends()
        if self.config.default_scene:
            await self.activate_scene(self.config.default_scene)
        self._publish(HubEvent(type="status", detail=f"Engine started with {len(self._backends)} device(s)"))

    async def stop(self) -> None:
        for task in list(self._pending.values()):
            task.cancel()
        self._pending.clear()
        self._press_at.clear()
        self._repeat_at.clear()
        self._credits.clear()
        self._ramp_elapsed.clear()
        self._focus = None
        await self._close_backends()

    async def reload(self, config: HubConfig) -> None:
        """Swaps in new configuration, rebuilding backends.

        The active scene is kept if it still exists, so saving an edit in the
        UI does not silently drop whatever is currently running. The focus is
        kept too -- switching what a device does with the buttons has nothing
        to do with which light was last touched -- unless the device that set
        it was itself removed, in which case there is nothing left to resolve
        the target against.
        """
        self.config = config
        await self._close_backends()
        await self._build_backends()
        if self.active_scene and not config.scene(self.active_scene):
            self.active_scene = None
        if self._focus and not config.device(self._focus.device):
            self._focus = None
        self._publish(HubEvent(type="status", detail="Configuration reloaded"))

    @property
    def focus(self) -> Optional[Focus]:
        """What the SmartHome +/- keys currently follow, for the API to report."""
        return self._focus

    async def _build_backends(self) -> None:
        for device in self.config.devices:
            try:
                backend = backends.create(device.backend, device.id, device.config)
                await backend.connect()
                self._backends[device.id] = backend
            except Exception as err:
                # One misconfigured device must not stop the others from
                # working; the failure surfaces in the UI's device list.
                logger.exception("Device '%s' failed to start", device.id)
                self._publish(
                    HubEvent(type="status", ok=False, detail=f"Device '{device.id}' failed to start: {err}")
                )

    async def _close_backends(self) -> None:
        for device_id, backend in self._backends.items():
            try:
                await backend.close()
            except Exception:
                logger.exception("Device '%s' failed to close cleanly", device_id)
        self._backends.clear()

    def backend_for(self, device_id: str) -> Optional[backends.Backend]:
        return self._backends.get(device_id)

    def _publish(self, event: HubEvent) -> None:
        self.broker.publish(event)

    # ------------------------------------------------------------------
    # Scenes
    # ------------------------------------------------------------------

    async def activate_scene(self, scene_id: str, depth: int = 0) -> None:
        """Stops whatever is running and starts `scene_id`.

        A switch between two scenes is not "stop everything, then start
        everything": the outgoing scene's stop macro goes through
        `_stop_actions` first, which drops the power-offs for any device the
        incoming scene still needs (or whose policy says it should stay up
        regardless). Without that, two scenes sharing a TV and an AV
        receiver -- "watch shieldTV" and "watch TV", say -- power-cycle both
        on every switch between them. `stop_scene()` is for going to idle,
        where there is no incoming scene to save a device from that filter.
        """
        scene = self.config.scene(scene_id)
        if scene is None:
            self._publish(HubEvent(type="scene", ok=False, detail=f"No such scene '{scene_id}'"))
            return
        if self.active_scene == scene_id:
            return

        leaving = self.config.scene(self.active_scene) if self.active_scene else None
        # Set before the outgoing stop macro runs, not after: a stop macro
        # that switches straight back to this same scene_id (a `SceneAction`
        # in `on_stop`, however unlikely) must see the switch as already
        # under way and no-op, the same as pressing an active scene's own
        # activity button does.
        self.active_scene = scene_id
        if leaving is not None:
            await self.run_actions(
                self._stop_actions(leaving, entering=scene),
                source=f"scene '{leaving.id}' stop",
                depth=depth,
            )

        self._publish(HubEvent(type="scene", scene=scene_id, ok=True, detail=f"Started {scene.name}"))
        await self.run_actions(self._start_actions(scene), source=f"scene '{scene_id}' start", depth=depth)

    async def stop_scene(self) -> None:
        """Runs the active scene's stop macro and returns to the idle state."""
        if not self.active_scene:
            return
        scene = self.config.scene(self.active_scene)
        stopped, self.active_scene = self.active_scene, None
        self._publish(HubEvent(type="scene", scene=None, ok=True, detail=f"Stopped {scene.name if scene else stopped}"))
        if scene:
            await self.run_actions(self._stop_actions(scene, entering=None), source=f"scene '{stopped}' stop")

    def _stop_actions(self, leaving: Scene, entering: Optional[Scene]) -> List[Action]:
        """`leaving.on_stop`, with power-offs skipped for devices that should
        stay powered through this transition.

        A device is spared if its policy says so outright -- `leave_on`
        survives any scene switch, `manual` is never touched by a scene macro
        at all -- or if `entering` still needs it, which is the case this
        exists for: two scenes sharing a TV should not power-cycle it just
        because the remote's user switched from one to the other.
        `entering=None` means there is nothing incoming to save a device --
        an explicit stop, or the off button -- so only the policy-based
        exemptions apply and a `managed` device still goes off.

        A stop macro that ends up with nothing left but delays is skipped
        outright rather than run: waiting accomplishes nothing once every
        action after the wait has been filtered out.
        """
        keep = {d.id for d in self.config.devices if d.power_policy is PowerPolicy.MANUAL}
        if entering is not None:
            keep |= entering.required_devices()
            keep |= {d.id for d in self.config.devices if d.power_policy is PowerPolicy.LEAVE_ON}

        kept: List[Action] = []
        for action in leaving.on_stop:
            device = self.config.device(action.device) if isinstance(action, DeviceAction) else None
            if device is not None and device.is_power_off(action.command) and device.id in keep:
                self._publish(
                    HubEvent(
                        type="action", action=action.describe(), ok=True,
                        detail=f"skipped -- {device.name} is still in use",
                    )
                )
                continue
            kept.append(action)

        if kept and all(isinstance(a, DelayAction) for a in kept):
            return []
        return kept

    def _start_actions(self, entering: Scene) -> List[Action]:
        """`entering.on_start`, with power commands dropped for `manual` devices.

        A `manual` device's policy promises it will never receive a power
        command the engine sent on its own initiative -- a scene macro is
        exactly that. A button bound straight to a power command is a
        different thing -- something the user asked for directly -- so
        `binding_for` is untouched; only what a scene does automatically is
        filtered here.
        """
        manual = {d.id for d in self.config.devices if d.power_policy is PowerPolicy.MANUAL}
        if not manual:
            return entering.on_start

        kept: List[Action] = []
        for action in entering.on_start:
            device = self.config.device(action.device) if isinstance(action, DeviceAction) else None
            if device is not None and device.id in manual and (
                device.is_power_on(action.command) or device.is_power_off(action.command)
            ):
                self._publish(
                    HubEvent(
                        type="action", action=action.describe(), ok=True,
                        detail=f"skipped -- {device.name} is set to manual power",
                    )
                )
                continue
            kept.append(action)
        return kept

    # ------------------------------------------------------------------
    # Button handling
    # ------------------------------------------------------------------

    def resolve_button(self, event: RemoteEvent) -> "tuple[str, str]":
        """The configuration key and display name for a press.

        The learned profile wins over the name the decoder produced: a button
        the operator called "SmartHome Bulb Upper" should read that way in
        the UI, not as the HID usage number it happens to transmit.

        An unrecognised signature falls back to its own hex, which keeps a
        newly-discovered button visible in the live view instead of vanishing
        -- that visibility is how it gets noticed and named.
        """
        profile = self.buttons.identify(event.signature)
        if profile is not None:
            return profile.key, profile.label
        return event.signature, event.name

    def binding_for(self, key: str) -> Optional[Binding]:
        """The binding in effect for a button right now, scene first.

        The fallback is another scene's bindings, not a separate set: nothing
        here needs to know that, since `global_scene` just names which scene
        to check when the active one (or none at all) does not bind the key.
        """
        if self.active_scene:
            scene = self.config.scene(self.active_scene)
            if scene and key in scene.bindings:
                return scene.bindings[key]
        if self.config.global_scene:
            fallback = self.config.scene(self.config.global_scene)
            if fallback:
                return fallback.bindings.get(key)
        return None

    async def handle(self, event: RemoteEvent) -> None:
        """Reacts to one event from the remote.

        Press/repeat/release dispatch runs exactly as normal even while
        paused -- hold-vs-tap timing and repeat throttling still track real
        elapsed time, so un-pausing mid-hold does not lose or misjudge it.
        What `paused` actually suppresses is downstream, in `run_actions`:
        the one place that ever reaches a backend, and the only choke point
        that also covers a hold timer armed before the pause was toggled.
        """
        key, label = self.resolve_button(event)
        binding = self.binding_for(key)

        if self.paused:
            detail = "paused"
        elif binding is None:
            detail = "unbound"
        else:
            detail = None

        self._publish(
            HubEvent(
                type="button",
                button=key,
                label=label,
                phase=event.kind,
                scene=self.active_scene,
                detail=detail,
            )
        )
        if binding is None:
            return

        if event.kind == "press":
            await self._on_press(key, binding)
        elif event.kind == "repeat":
            # While a hold decision is outstanding the press has not been
            # dispatched yet, so repeating it would fire actions out of order.
            if key not in self._pending:
                count = self._repeats_due(key, binding)
                for i in range(count):
                    await self.run_actions(
                        binding.on_repeat,
                        source=f"{key}.repeat",
                        announce=(i == 0),
                        label_suffix=f" ×{count}" if i == 0 and count > 1 else "",
                    )
        elif event.kind == "release":
            await self._on_release(key, binding)

    def _repeats_due(self, key: str, binding: Binding) -> int:
        """How many times this packet's repeat actions should fire.

        Ordinarily 0 or 1: the remote reports a held button roughly every
        100ms and says nothing about how long it has been down, so without a
        delay an ordinary 300ms press fires the repeat actions three times
        over. Waiting is the only thing that distinguishes "held" from
        "pressed" -- the same reason a keyboard waits before it starts
        repeating a character.

        `repeat_accel` layers a ramp on top of that. The remote never reports
        a held button any faster than that ~100ms cadence, so once
        `repeat_interval` is already at that ceiling there is no faster
        packet to wait for -- the only way to go faster still is to run the
        repeat actions more than once for a single packet. `_credits` banks
        fractional progress between packets so the count climbs smoothly
        rather than jumping in whole-repeat steps, and `_ramp_elapsed` tracks
        how long the ramp itself has been running -- separately from
        wall-clock "time held" -- so a burst of packets queued up behind a
        slow backend cannot all cash in at the top of the ramp the instant
        the backend catches up.

        A binding that leaves any of these unset -- the common case --
        follows the config-wide default instead. Only a binding that sets
        its own overrides it.
        """
        delay = binding.repeat_delay
        if delay is None:
            delay = self.config.default_repeat_delay
        interval = binding.repeat_interval
        if interval is None:
            interval = self.config.default_repeat_interval
        accel = binding.repeat_accel
        if accel is None:
            accel = self.config.default_repeat_accel
        accel_seconds = binding.repeat_accel_seconds
        if accel_seconds is None:
            accel_seconds = self.config.default_repeat_accel_seconds

        now = time.monotonic()
        # A repeat with no press behind it -- the press packet was lost, or
        # the button was already down when the hub started -- counts as
        # starting now, so the ramp is late rather than instant.
        started = self._press_at.setdefault(key, now)
        if now - started < delay:
            return 0

        last = self._repeat_at.get(key)
        self._repeat_at[key] = now
        if last is None:
            # The first packet past the delay always fires exactly once --
            # there is no real "since last packet" gap yet to build a rate
            # or a ramp from, and a plain (non-accelerated) binding should
            # behave exactly as it always has.
            self._credits[key] = 0.0
            self._ramp_elapsed[key] = 0.0
            return 1

        dt = min(now - last, MAX_REPEAT_DT)

        multiplier = 1.0
        if accel > 1.0:
            ramp = self._ramp_elapsed.get(key, 0.0) + dt
            self._ramp_elapsed[key] = ramp
            progress = min(ramp / accel_seconds, 1.0)
            multiplier = accel**progress

        if interval > 0:
            gain = dt / interval
        else:
            # No throttle: one credit per packet at the base rate,
            # independent of exactly how far apart packets land -- real
            # packet spacing jitters by a few milliseconds and must not gate
            # whether this fires.
            gain = 1.0

        credits = self._credits.get(key, 0.0) + gain * multiplier
        count = min(int(credits), MAX_REPEAT_BURST)
        self._credits[key] = credits - count
        return count

    async def _on_press(self, key: str, binding: Binding) -> None:
        self._press_at[key] = time.monotonic()
        self._repeat_at.pop(key, None)
        self._credits.pop(key, None)
        self._ramp_elapsed.pop(key, None)

        if not binding.on_hold:
            await self.run_actions(binding.on_press, source=f"{key}.press")
            return

        # With a hold action configured, a short press and a long press are
        # indistinguishable until the button is released or the timer expires,
        # so the tap has to wait. Only buttons that actually use a hold pay
        # this latency.
        self._hold_fired[key] = False
        self._pending[key] = asyncio.create_task(self._await_hold(key, binding))

    async def _await_hold(self, key: str, binding: Binding) -> None:
        try:
            await asyncio.sleep(binding.hold_seconds)
        except asyncio.CancelledError:
            return
        self._hold_fired[key] = True
        self._pending.pop(key, None)
        await self.run_actions(binding.on_hold, source=f"{key}.hold")

    async def _on_release(self, key: str, binding: Binding) -> None:
        self._press_at.pop(key, None)
        self._repeat_at.pop(key, None)
        self._credits.pop(key, None)
        self._ramp_elapsed.pop(key, None)

        task = self._pending.pop(key, None)
        if task is not None:
            task.cancel()
        if binding.on_hold and not self._hold_fired.pop(key, True):
            # Released before the hold timer: it was a tap after all.
            await self.run_actions(binding.on_press, source=f"{key}.press")
        await self.run_actions(binding.on_release, source=f"{key}.release")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def run_actions(
        self,
        actions: List[Action],
        source: str,
        depth: int = 0,
        *,
        announce: bool = True,
        label_suffix: str = "",
    ) -> None:
        """Runs a macro in order, reporting each step.

        Errors are reported and the macro continues. A power-on that fails
        should not prevent the input-select that follows it from being tried,
        and stopping halfway would leave equipment in a worse state than
        finishing does.

        The single choke point `self.paused` uses: every action of every
        kind -- a direct press, a fired hold, a scene's own start/stop macro
        -- passes through here on its way to a backend, so gating here
        (rather than earlier, at the button-handling level) is what also
        catches a hold timer that was already armed before pausing.

        `announce` and `label_suffix` exist for an accelerated repeat burst:
        `_repeats_due` can call this several times for one packet, and
        logging every one of those individually would flood the live view
        for no benefit -- a `SwitchListTile` slider does not need proof that
        volume went up eight times instead of four. The first call in a
        burst passes `label_suffix=" ×8"` so the log shows what actually
        happened; the rest pass `announce=False` and run silently. Failures
        are never silenced, on any call -- something going wrong is exactly
        what the log is for.
        """
        if self.paused:
            if announce:
                for action in actions:
                    self._publish(
                        HubEvent(
                            type="action",
                            action=action.describe() + label_suffix,
                            ok=True,
                            detail=f"{source}: paused -- not executed",
                        )
                    )
            return

        if depth > MAX_ACTION_DEPTH:
            if announce:
                self._publish(
                    HubEvent(type="action", ok=False, detail=f"{source}: scene actions nested too deeply, stopping")
                )
            return

        for action in actions:
            try:
                await self._run_action(action, source, depth, announce=announce, label_suffix=label_suffix)
            except Exception as err:
                logger.exception("%s: %s failed", source, action.describe())
                self._publish(
                    HubEvent(
                        type="action", action=action.describe() + label_suffix, ok=False, detail=f"{source}: {err}"
                    )
                )

    async def _run_action(
        self, action: Action, source: str, depth: int, *, announce: bool = True, label_suffix: str = ""
    ) -> None:
        if isinstance(action, DelayAction):
            await asyncio.sleep(action.seconds)
            return

        if isinstance(action, SceneAction):
            if action.scene is None:
                await self.stop_scene()
            elif self.config.scene(action.scene) is None:
                raise KeyError(f"unknown scene '{action.scene}'")
            else:
                await self.activate_scene(action.scene, depth=depth + 1)
            return

        if isinstance(action, AdjustAction):
            await self._run_adjust(action, source, announce=announce, label_suffix=label_suffix)
            return

        assert isinstance(action, DeviceAction)
        backend = self._backends.get(action.device)
        if backend is None:
            raise KeyError(f"device '{action.device}' is not running")

        await asyncio.wait_for(backend.send(action.command, action.params), timeout=ACTION_TIMEOUT)
        if announce:
            self._publish(HubEvent(type="action", action=action.describe() + label_suffix, ok=True, detail=source))
        self._update_focus(action.device, backend, action.command)

    def _update_focus(self, device_id: str, backend: "backends.Backend", command: str) -> None:
        """Lets the device just touched claim the SmartHome +/- keys.

        Only a backend that recognises `command` as having acted on
        something moves the focus; most backends answer `None` here, which
        is why pressing Volume Up on the Shield does not steal the keys away
        from a light that was touched earlier. Re-touching the same target
        -- holding a repeatable command like a media player's volume -- is
        announced once, not on every repeat, since nothing about the focus
        actually changed.
        """
        target = backend.focus_for(command)
        if target is None:
            return
        if self._focus and (self._focus.device, self._focus.target) == (device_id, target.target):
            return
        self._focus = Focus(device=device_id, target=target.target, label=target.label)
        self._publish(HubEvent(type="status", detail=f"SmartHome +/- now follow {target.label}"))

    async def _run_adjust(
        self, action: AdjustAction, source: str, *, announce: bool = True, label_suffix: str = ""
    ) -> None:
        """Steps whatever is focused, up or down.

        Falls back to the action's own `device`/`target` only when nothing
        has been touched yet -- right after a restart, say. Failures are
        raised rather than reported here, the same as an unreachable device
        would be for an ordinary `DeviceAction`, so `run_actions` logs and
        publishes them uniformly and the rest of the macro still runs.
        """
        if self._focus is not None:
            device_id, target, label = self._focus.device, self._focus.target, self._focus.label
        elif action.device and action.target:
            device_id, target, label = action.device, action.target, action.target
        else:
            raise backends.BackendError("nothing has been touched yet -- press a SmartHome key first")

        backend = self._backends.get(device_id)
        if backend is None:
            raise KeyError(f"device '{device_id}' is not running")

        command = backend.adjust_command(target, action.direction)
        if command is None:
            raise backends.BackendError(f"{label} has nothing to turn {action.direction}")

        await asyncio.wait_for(backend.send(command), timeout=ACTION_TIMEOUT)
        if announce:
            self._publish(
                HubEvent(
                    type="action", action=f"{device_id}.{command}{label_suffix}", ok=True,
                    detail=f"{source} · following {label}",
                )
            )
