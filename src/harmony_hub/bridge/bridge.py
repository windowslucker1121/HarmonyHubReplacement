"""The running bridge: one reconnecting MQTT session, and nothing more.

Owned by `HubRuntime` rather than `HubService` -- the same split as
`EventBroker` -- because Home Assistant needs to see this hub as `offline`
precisely when the hub itself has failed or is stopped, not lose the device
entirely the moment `HubService` is torn down. So this runs independently of
whether the hub is up, connects on its own schedule, and reads
`HubRuntime`/`SceneEngine` state fresh on every publish rather than being
handed a snapshot.

**Never raises out of `start()`/`stop()`/`reconfigure()`**, the same
contract `HubRuntime.start()` keeps for the hub itself: a broker that is
unreachable, misconfigured, or simply switched off must not be able to take
the rest of the process down with it. Connection failures are caught inside
the reconnect loop and reported through `self.detail`, `/api/mqtt`, and the
ordinary hub event log -- never raised across the task boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from ..events import HubEvent
from ..models import DeviceAction
from . import discovery, state_file
from .client import build_client
from .credentials import read_password
from .topics import CMD_ACTIVITY, CMD_PAUSED, CMD_RUNNING, CMD_SCENE_PREFIX, CMD_SEND, Topics

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import HubRuntime

logger = logging.getLogger("HUB.bridge")

#: How long between reconnect attempts, doubling on every consecutive
#: failure up to this ceiling -- a broker that is down for an hour should
#: not be hammered every second for that whole hour.
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 60.0

#: How often the state document and discovery document are re-checked even
#: with no hub event to prompt it -- the backstop for a fact that can change
#: on its own, like a backend's `health()` cache expiring. Short enough that
#: a device flipping available/unavailable shows up in Home Assistant
#: within one glance at a dashboard, long enough not to matter as traffic.
TICK_SECONDS = 20.0

#: A message on `cmd/paused` or `cmd/running` this loose is deliberate: an
#: MQTT `switch`'s default `payload_on` is `"ON"`, but a hand-written
#: automation might send `"true"` or `"1"`, and none of them should require
#: the user to discover the one exact string that works.
_TRUE_PAYLOADS = {"on", "true", "1"}

ClientFactory = Callable[[Any, str, Topics], Any]


class MqttBridge:
    """Publishes this hub into Home Assistant, and carries out what comes back.

    `client_factory` exists purely for tests: it is called with
    `(settings, password, topics)` and must return an async context manager
    that, once entered, offers `publish`, `subscribe` and a `messages`
    async-iterable property -- exactly `aiomqtt.Client`'s shape. Left
    `None`, it defaults to `bridge.client.build_client`; a test supplies a
    fake instead, the same way `test_hub_backends_ir.py` supplies a fake
    `pigpio` rather than opening a real one.
    """

    def __init__(self, runtime: "HubRuntime", client_factory: Optional[ClientFactory] = None) -> None:
        self._runtime = runtime
        self._client_factory = client_factory or build_client
        self._task: Optional[asyncio.Task] = None
        self._topics: Optional[Topics] = None
        self._last_components: Dict[str, str] = {}
        self._last_fingerprint: Optional[str] = None
        #: The live session, only while `_session` is running -- what
        #: `republish()` publishes over. `None` the rest of the time, which
        #: is what tells `republish()` there is nothing to publish over.
        self._client: Optional[Any] = None

        #: What `/api/mqtt` reports. `connected` is `False` any time this
        #: bridge is not actively holding an open session -- disabled,
        #: mid-backoff, or never configured -- so the settings screen has
        #: one boolean to render a dot from.
        self.connected: bool = False
        self.detail: str = "disabled"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Opens the background connection loop, if settings say to and it is not already running."""
        if self._task is not None:
            return
        if not self._runtime.settings.mqtt_enabled:
            self.detail = "disabled"
            return
        self.detail = "connecting…"
        self._task = asyncio.create_task(self._run(), name="mqtt-bridge")

    async def stop(self) -> None:
        """Closes the connection loop. Safe to call whether or not one is running."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        self.connected = False
        self.detail = "disabled"

    async def reconfigure(self) -> None:
        """Applies changed MQTT settings by restarting the connection loop onto them.

        Unconditional stop-then-start rather than diffing what changed:
        `HubRuntime.apply_settings` already only calls this when a
        connection-relevant field actually moved, and a fresh connection is
        the only way a changed host, port or credential could take effect
        anyway.
        """
        await self.stop()
        self.start()

    async def republish(self) -> bool:
        """Forces a fresh discovery and state publish over the current session, if there is one.

        For a "Republish" button in Settings: Home Assistant's own database
        of retained messages can be cleared independently of this hub (a
        broker restart with a clean session, `mosquitto`'s retained-message
        store being wiped, someone deleting the device by hand), and
        waiting on the next `TICK_SECONDS` tick or hub event is not always
        good enough for "I just checked and it's not there". Unlike the
        tick, this bypasses `discovery.fingerprint`'s unchanged-skip on
        purpose. Returns whether there was a connection to publish over.
        """
        if self._client is None:
            return False
        self._last_fingerprint = None
        await self._maybe_publish_discovery(self._client)
        await self._publish_state(self._client)
        return True

    # ------------------------------------------------------------------
    # The reconnect loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        settings = self._runtime.settings
        if not settings.mqtt_host:
            self.detail = "no broker host configured"
            logger.info("MQTT bridge is enabled but no broker host is set")
            return

        self._topics = Topics(node_id=settings.mqtt_node_id, discovery_prefix=settings.mqtt_discovery_prefix)
        self._last_components = state_file.load_components()
        password = read_password(settings.mqtt_node_id)

        backoff = INITIAL_BACKOFF
        while True:
            try:
                client = self._client_factory(settings, password, self._topics)
                async with client as session:
                    self.connected = True
                    self.detail = f"connected to {settings.mqtt_host}:{settings.mqtt_port}"
                    logger.info("MQTT bridge connected to %s:%s", settings.mqtt_host, settings.mqtt_port)
                    backoff = INITIAL_BACKOFF
                    await self._session(session)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logger.warning("MQTT bridge: %s", err)
                self.detail = str(err) or type(err).__name__
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, MAX_BACKOFF)

    async def _session(self, client: Any) -> None:
        """One connected session: subscribe, announce, then serve until the connection drops."""
        await client.subscribe(self._topics.cmd_wildcard, qos=1)
        await self._publish_availability(client, online=True)
        await self._maybe_publish_discovery(client)
        await self._publish_state(client)
        self._client = client
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._listen_commands(client))
                tg.create_task(self._listen_hub_events(client))
                tg.create_task(self._tick(client))
        finally:
            self._client = None
            # Best-effort: the connection may already be the thing that
            # died. The Will (see `client.build_client`) is what covers
            # that case; this is only for the clean-shutdown path, where it
            # beats waiting on the broker's keepalive timeout.
            with contextlib.suppress(Exception):
                await self._publish_availability(client, online=False)

    async def _tick(self, client: Any) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            await self._maybe_publish_discovery(client)
            await self._publish_state(client)

    # ------------------------------------------------------------------
    # Outbound: hub events -> MQTT
    # ------------------------------------------------------------------

    async def _listen_hub_events(self, client: Any) -> None:
        async for event in self._runtime.broker.subscribe():
            if event.type == "button":
                await self._maybe_publish_button_event(client, event)
            if event.type in ("button", "scene", "hub", "status"):
                await self._publish_state(client)
            if event.type == "status":
                # Covers a config reload and a button learned/forgotten --
                # both are reported as "status" events by `HubRuntime`/
                # `SceneEngine`, and re-checking discovery is cheap: see
                # `discovery.fingerprint`, which is what keeps an
                # unrelated status event from publishing anything.
                await self._maybe_publish_discovery(client)

    async def _maybe_publish_button_event(self, client: Any, event: HubEvent) -> None:
        if not event.button:
            return
        if event.phase == "repeat" and not self._runtime.settings.mqtt_publish_repeats:
            return
        doc = discovery.button_event(
            key=event.button, label=event.label or event.button, phase=event.phase or "press", scene=event.scene
        )
        await client.publish(self._topics.button_event, json.dumps(doc).encode(), qos=0, retain=False)

    async def _publish_availability(self, client: Any, *, online: bool) -> None:
        await client.publish(self._topics.availability, b"online" if online else b"offline", qos=1, retain=True)

    async def _maybe_publish_discovery(self, client: Any) -> None:
        settings = self._runtime.settings
        doc = discovery.payload(
            self._topics,
            self._runtime.config,
            self._runtime.buttons,
            device_name=settings.mqtt_device_name or "Harmony Hub",
            sw_version=self._sw_version(),
            configuration_url=self._configuration_url(),
        )
        fp = discovery.fingerprint(doc)
        if fp == self._last_fingerprint:
            return

        new_components = doc["components"]
        removed = discovery.diff_removed(self._last_components, new_components)
        if removed:
            # Home Assistant does not infer a removal from a component key
            # simply being absent on a republish -- it has to be announced
            # with an empty (platform-only) config first, and only then
            # omitted. See `discovery.diff_removed`'s docstring.
            interim = dict(doc)
            interim_components = dict(new_components)
            for key, platform in removed.items():
                interim_components[key] = {"platform": platform}
            interim["components"] = interim_components
            await client.publish(self._topics.discovery, json.dumps(interim).encode(), qos=1, retain=True)

        await client.publish(self._topics.discovery, json.dumps(doc).encode(), qos=1, retain=True)

        self._last_components = {key: comp["platform"] for key, comp in new_components.items()}
        self._last_fingerprint = fp
        state_file.save_components(self._last_components)

    async def _publish_state(self, client: Any) -> None:
        doc = await self._state_dict()
        await client.publish(self._topics.state, json.dumps(doc).encode(), qos=0, retain=True)

    async def _state_dict(self) -> Dict[str, Any]:
        runtime = self._runtime
        engine = runtime.service.engine if runtime.service else None
        status = runtime.status()
        focus = engine.focus if engine else None
        devices = await runtime.device_statuses()
        return discovery.state(
            hub_state=status.state,
            hub_detail=status.detail,
            active_scene=engine.active_scene if engine else None,
            paused=engine.paused if engine else False,
            running=runtime.service is not None,
            focus_label=focus.label if focus else None,
            devices=devices,
        )

    def _sw_version(self) -> str:
        try:
            from importlib.metadata import version

            return version("harmony-receiver")
        except Exception:
            return ""

    def _configuration_url(self) -> str:
        host = self._runtime.settings.host
        if not host or host in ("0.0.0.0", "::"):
            return ""
        return f"http://{host}:{self._runtime.settings.port}/"

    # ------------------------------------------------------------------
    # Inbound: MQTT -> hub commands
    # ------------------------------------------------------------------

    async def _listen_commands(self, client: Any) -> None:
        async for message in client.messages:
            topic = str(message.topic)
            if not topic.startswith(self._topics.cmd_root):
                continue
            rest = topic[len(self._topics.cmd_root) :]
            payload = _decode(message.payload)
            try:
                await self._handle_command(rest, payload)
            except Exception as err:
                logger.warning("MQTT command on %s failed: %s", topic, err)
                self._runtime.broker.publish(
                    HubEvent(type="status", ok=False, detail=f"Home Assistant command '{rest}' failed: {err}")
                )

    async def _handle_command(self, rest: str, payload: str) -> None:
        if rest == CMD_ACTIVITY:
            await self._cmd_activate(payload)
        elif rest.startswith(CMD_SCENE_PREFIX):
            await self._cmd_activate(rest[len(CMD_SCENE_PREFIX) :])
        elif rest == CMD_PAUSED:
            self._cmd_paused(payload)
        elif rest == CMD_RUNNING:
            await self._cmd_running(payload)
        elif rest == CMD_SEND:
            await self._cmd_send(payload)
        else:
            logger.warning("Unknown MQTT command topic suffix: %s", rest)

    def _engine(self):
        engine = self._runtime.service.engine if self._runtime.service else None
        if engine is None:
            raise RuntimeError("the hub is not running -- start it from Settings")
        return engine

    async def _cmd_activate(self, scene_id: str) -> None:
        scene_id = scene_id.strip()
        engine = self._engine()
        if not scene_id or scene_id == discovery.OFF_OPTION:
            await engine.stop_scene()
            return
        if self._runtime.config.scene(scene_id) is None:
            raise ValueError(f"no such scene '{scene_id}'")
        await engine.activate_scene(scene_id)

    def _cmd_paused(self, payload: str) -> None:
        engine = self._engine()
        engine.paused = payload.strip().lower() in _TRUE_PAYLOADS

    async def _cmd_running(self, payload: str) -> None:
        if payload.strip().lower() in _TRUE_PAYLOADS:
            await self._runtime.start()
        else:
            await self._runtime.stop()

    async def _cmd_send(self, payload: str) -> None:
        engine = self._engine()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as err:
            raise ValueError(f"not valid JSON: {err}") from err
        device = str(data.get("device") or "").strip()
        command = str(data.get("command") or "").strip()
        if not device or not command:
            raise ValueError("expected {\"device\": ..., \"command\": ..., \"params\": {...}}")
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("'params' must be an object")
        action = DeviceAction(device=device, command=command, params=params)
        await engine.run_actions([action], source="Home Assistant")


def _decode(payload: Any) -> str:
    if isinstance(payload, (bytes, bytearray)):
        return payload.decode("utf-8", "replace").strip()
    return str(payload).strip()
