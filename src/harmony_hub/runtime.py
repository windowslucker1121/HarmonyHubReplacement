"""The supervisor: everything that outlives a hub restart.

The web layer starts once and is never touched again. Underneath it, the hub
itself -- engine, backends, radio -- is a child that can be stopped, started,
reconfigured and restarted while the page stays up. That separation is the
entire point of this module, and it rests on one rule:

    **`start()` never raises.**

A missing FT232H, a typo'd address, a `hub_config.json` someone hand-edited
badly: each of those used to escape FastAPI's lifespan and stop uvicorn
serving anything at all, which meant the one screen that could explain the
failure was the screen that failed to load. Here they become a `failed`
state, an error string, and an event -- all of them things the settings page
can render.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from harmony_receiver.profiles import ButtonMap
from pydantic import BaseModel

from . import backends
from . import config as config_module
from . import settings as settings_module
from .bridge import MqttBridge
from .discovery import DiscoveryJob, DiscoveryMethod, DiscoveryStatus, DEFAULT_SNIFF_VERIFY_TIMEOUT
from .events import EventBroker, HubEvent
from .ir import gateway as ir_gateway
from .models import HubConfig
from .service import HubService
from .settings import HubSettings

logger = logging.getLogger("HUB.runtime")

RuntimeState = Literal["stopped", "starting", "running", "failed"]


class RuntimeStatus(BaseModel):
    """What the hub is doing, flat enough for the settings page to render directly."""

    state: RuntimeState
    detail: str = ""
    source: str = ""
    started_at: Optional[datetime] = None

    #: Where the live server is actually listening. Not necessarily what
    #: `settings.host`/`port` now say -- see `pending_restart`.
    host: str
    port: int

    #: Saved bind settings differ from the ones this process started with, so
    #: they will not take effect until it is restarted. Surfaced rather than
    #: silently ignored, because "I changed the port and nothing happened" is
    #: otherwise indistinguishable from a bug.
    pending_restart: bool = False

    settings_path: str = ""
    config_path: str = ""

    #: Set when the configuration file could not be read. The hub still
    #: serves, but with an empty configuration standing in for the real one.
    config_error: Optional[str] = None
    settings_error: Optional[str] = None

    problems: list[str] = []


class HubRuntime:
    """Owns the settings, configuration, buttons and broker across restarts."""

    def __init__(
        self,
        settings: Optional[HubSettings] = None,
        settings_path: str | Path = settings_module.DEFAULT_PATH,
        settings_error: Optional[str] = None,
    ) -> None:
        self.settings = settings or HubSettings()
        self.settings_path = Path(settings_path)
        self.settings_error = settings_error

        # What this process actually bound. Kept so a later host/port edit can
        # be reported as pending rather than pretending to have applied.
        self.launched_with = self.settings.model_copy(deep=True)

        # One broker for the life of the process. Restarting the hub must not
        # blank the activity log -- that log is what someone is reading in
        # order to work out why they are restarting it.
        self.broker = EventBroker()

        self.config: HubConfig = HubConfig()
        self.config_error: Optional[str] = None
        self.buttons: ButtonMap = ButtonMap()
        self.service: Optional[HubService] = None

        self.state: RuntimeState = "stopped"
        self.detail: str = "Not started yet"
        self.started_at: Optional[datetime] = None
        self.discovery = DiscoveryJob(self.settings.csn_pin, self.settings.ce_pin)

        # Independent of the hub's own lifecycle -- see `bridge.MqttBridge`'s
        # docstring for why it is owned here rather than by `HubService`.
        # `create_app`'s lifespan starts and stops it alongside the hub.
        self.mqtt_bridge = MqttBridge(self)

        # start/stop/restart are serialised: two concurrent restarts must not
        # interleave and leave half an engine behind.
        self._lock = asyncio.Lock()

        self.reload_config()
        self.reload_buttons()

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def reload_config(self) -> None:
        """Re-reads the configuration, surviving a file that will not parse.

        A malformed file leaves an empty configuration standing in, so the
        hub still serves and the problem is visible. `save_config` then
        refuses to overwrite it without being told to, because writing that
        empty stand-in back over a recoverable file would delete every scene
        the user owns.
        """
        try:
            self.config = config_module.load(self.settings.config_path)
            self.config_error = None
        except Exception as err:
            logger.error("Could not read %s: %s", self.settings.config_path, err)
            self.config = HubConfig()
            self.config_error = f"{self.settings.config_path} could not be read: {err}"

    def reload_buttons(self) -> ButtonMap:
        """Re-reads buttons.json, picking up names learned since startup."""
        try:
            self.buttons = ButtonMap.load(self.settings.buttons_path)
        except Exception as err:
            logger.error("Could not read %s: %s", self.settings.buttons_path, err)
            self.buttons = ButtonMap()
        if self.service is not None:
            self.service.apply_buttons(self.buttons)
        return self.buttons

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Brings the hub up. Never raises; returns whether it is running."""
        async with self._lock:
            return await self._start()

    async def stop(self) -> None:
        """Takes the hub down. Never raises; safe to call when already stopped."""
        async with self._lock:
            await self._stop("Stopped")

    async def restart(self) -> bool:
        async with self._lock:
            await self._stop("Restarting")
            return await self._start()

    async def _start(self) -> bool:
        if self.service is not None:
            return True

        if self.discovery.running:
            # The radio is on a worker thread mid-search; opening it again
            # here would be the exact contention `start_discovery` stops the
            # hub to avoid in the first place.
            return self._fail("a remote search is in progress -- wait for it to finish, or cancel it")

        problems = self.settings.problems()
        if problems:
            # Refused rather than attempted: these are the failures we can
            # describe precisely, and a precise message beats whatever the
            # radio stack would have raised three layers down.
            return self._fail(" ".join(problems))

        self._set_state("starting", "Starting…")
        service = HubService(self.settings, self.config, self.buttons, self.broker)
        try:
            await service.start()
        except Exception as err:
            logger.exception("Hub failed to start")
            # Tear down whatever did come up, so a retry is not fighting a
            # half-built engine holding the radio.
            try:
                await service.stop()
            except Exception:
                logger.exception("Cleaning up after a failed start also failed")
            return self._fail(str(err))

        self.service = service
        self.started_at = datetime.now()
        self._set_state("running", self.settings.describe_source())
        return True

    async def _stop(self, detail: str) -> None:
        service, self.service = self.service, None
        if service is not None:
            try:
                await service.stop()
            except Exception:
                logger.exception("Hub failed to stop cleanly")
        self.started_at = None
        self._set_state("stopped", detail)

    def _fail(self, detail: str) -> bool:
        self.service = None
        self.started_at = None
        self._set_state("failed", detail, ok=False)
        return False

    def _set_state(self, state: RuntimeState, detail: str, ok: bool = True) -> None:
        self.state = state
        self.detail = detail
        logger.info("Hub is %s: %s", state, detail)
        self.broker.publish(HubEvent(type="hub", ok=ok, detail=f"{state}: {detail}"))

    # ------------------------------------------------------------------
    # Applying changes
    # ------------------------------------------------------------------

    async def apply_settings(self, new_settings: HubSettings, restart: bool = False) -> None:
        """Persists settings and, if asked, restarts the hub onto them.

        Bind host and port are saved but not applied: rebinding the live
        listener would move the page's URL out from under whoever is editing
        it. `RuntimeStatus.pending_restart` says so.

        IR pins are the opposite case: unlike the radio's CSN/CE, which are
        read once when a `RadioSource` opens and so need the hub restarted
        to move, `ir_gateway.reconfigure` tears down and reopens the one
        shared gateway connection in place. So a pin change is applied
        immediately below rather than folded into `pending_restart` -- that
        is the whole point of it being editable on the fly.
        """
        files_moved = (
            new_settings.config_path != self.settings.config_path
            or new_settings.buttons_path != self.settings.buttons_path
        )
        ir_changed = (
            new_settings.ir_rx_pin != self.settings.ir_rx_pin
            or new_settings.ir_tx_pin != self.settings.ir_tx_pin
            or new_settings.ir_pigpio_host != self.settings.ir_pigpio_host
            or new_settings.ir_pigpio_port != self.settings.ir_pigpio_port
        )
        # Like IR pins, unlike host/port: `MqttBridge` is not read once at
        # process start, so a changed broker takes effect by reconnecting
        # rather than by needing `restart`. Independent of `self.service`
        # (unlike `ir_changed` above) because the bridge runs whether or not
        # the hub itself is up -- see its docstring.
        mqtt_changed = (
            new_settings.mqtt_enabled != self.settings.mqtt_enabled
            or new_settings.mqtt_host != self.settings.mqtt_host
            or new_settings.mqtt_port != self.settings.mqtt_port
            or new_settings.mqtt_username != self.settings.mqtt_username
            or new_settings.mqtt_tls != self.settings.mqtt_tls
            or new_settings.mqtt_discovery_prefix != self.settings.mqtt_discovery_prefix
            or new_settings.mqtt_node_id != self.settings.mqtt_node_id
            or new_settings.mqtt_device_name != self.settings.mqtt_device_name
        )

        settings_module.save(new_settings, self.settings_path)
        self.settings = new_settings
        self.settings_error = None

        if files_moved:
            self.reload_config()
            self.reload_buttons()

        if restart:
            await self.restart()
        else:
            if files_moved and self.service is not None:
                await self.service.apply_config(self.config)
                self.service.apply_buttons(self.buttons)
            if ir_changed and self.service is not None:
                ir_gateway.reconfigure(self.settings)

        if mqtt_changed:
            await self.mqtt_bridge.reconfigure()

    async def save_config(self, new_config: HubConfig, force: bool = False) -> None:
        """Persists configuration and applies it to the hub if one is running.

        Works while the hub is stopped: editing scenes is not something that
        should require the equipment to be reachable.
        """
        if self.config_error and not force:
            raise ConfigUnreadable(self.config_error)

        config_module.save(new_config, self.settings.config_path)
        self.config = new_config
        self.config_error = None
        if self.service is not None:
            await self.service.apply_config(new_config)

    def simulate(self, key: str, kind: str = "press"):
        if self.service is None:
            raise HubNotRunning()
        return self.service.simulate(key, kind)

    async def device_statuses(self) -> "list[dict]":
        """Health of every configured device, backend-agnostic.

        Shared by `/api/state` and `bridge.MqttBridge`, which both need the
        same "is this thing reachable" answer -- spelled out once here
        rather than twice, so the two can never quietly disagree about what
        a device's `ok`/`detail` means.
        """
        statuses = []
        for device in self.config.devices:
            backend = self.service.engine.backend_for(device.id) if self.service else None
            if backend is None:
                statuses.append(
                    {
                        "id": device.id, "name": device.name, "backend": device.backend,
                        "running": False, "ok": False,
                        "detail": "not started" if self.service else "the hub is stopped",
                    }
                )
                continue
            try:
                health = await backend.health()
            except Exception as err:
                health = backends.Health(ok=False, detail=str(err))
            statuses.append(
                {
                    "id": device.id, "name": device.name, "backend": device.backend,
                    "running": True, "ok": health.ok, "detail": health.detail,
                }
            )
        return statuses

    # ------------------------------------------------------------------
    # Learning buttons
    #
    # A signature only becomes a *button* when a human says what it is
    # called, so both of these are the UI writing down that answer. They work
    # with the hub stopped for the same reason configuration does: naming
    # what you already captured needs no radio.
    # ------------------------------------------------------------------

    def learn_buttons(self, learned: "list[tuple[str, str, list[str]]]") -> ButtonMap:
        """Records `(key, label, signatures)` triples into the button map.

        Adding a signature to a key that already exists is deliberate rather
        than an error: one physical button can report differently depending
        on the active activity, and both signatures belong to the same name.
        """
        for key, label, signatures in learned:
            for signature in signatures:
                self.buttons.learn(key, label, signature)

        self.buttons.save(self.settings.buttons_path)
        if self.service is not None:
            self.service.apply_buttons(self.buttons)

        names = ", ".join(key for key, _, _ in learned)
        self.broker.publish(
            HubEvent(type="status", ok=True, detail=f"Learned {len(learned)} button(s): {names}")
        )
        return self.buttons

    def forget_button(self, key: str) -> ButtonMap:
        """Drops a button and every signature it owned."""
        self.buttons.forget(key)
        self.buttons.save(self.settings.buttons_path)
        if self.service is not None:
            self.service.apply_buttons(self.buttons)
        self.broker.publish(HubEvent(type="status", ok=True, detail=f"Forgot button '{key}'"))
        return self.buttons

    # ------------------------------------------------------------------

    async def start_discovery(
        self,
        timeout: float,
        method: DiscoveryMethod = "hub",
        verify_timeout: float = DEFAULT_SNIFF_VERIFY_TIMEOUT,
    ) -> DiscoveryStatus:
        """Begins searching for the remote's address, freeing the radio first if needed.

        A hub already listening on the radio would collide with the search
        over the same nRF24, so it is stopped for the duration rather than
        refusing to search at all -- and started again once the search ends,
        found or not, so "stop the hub, search, start it again" collapses
        into one button. The restore happens here rather than being left to
        the client, so it still runs even if the tab is closed or the
        connection drops mid-search.
        """
        if self.discovery.running:
            return self.discovery.status

        resume = self.service is not None and self.service.uses_radio
        if resume:
            await self.stop()

        self.discovery.csn_pin = self.settings.csn_pin
        self.discovery.ce_pin = self.settings.ce_pin
        return self.discovery.start(
            timeout,
            method=method,
            verify_timeout=verify_timeout,
            on_finish=self._resume_after_discovery if resume else None,
        )

    async def _resume_after_discovery(self) -> None:
        await self.start()

    # ------------------------------------------------------------------

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            state=self.state,
            detail=self.detail,
            source=self.settings.describe_source(),
            started_at=self.started_at,
            host=self.launched_with.host,
            port=self.launched_with.port,
            pending_restart=self.settings.needs_process_restart(self.launched_with),
            settings_path=str(self.settings_path),
            config_path=str(self.settings.config_path),
            config_error=self.config_error,
            settings_error=self.settings_error,
            problems=self.settings.problems(),
        )


class HubNotRunning(RuntimeError):
    """Raised where an operation genuinely needs a live engine."""

    def __init__(self) -> None:
        super().__init__("the hub is not running -- start it from Settings")


class ConfigUnreadable(RuntimeError):
    """Raised rather than overwriting a configuration file that would not parse."""
