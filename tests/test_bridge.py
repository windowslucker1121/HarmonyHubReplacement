"""The running MQTT bridge, driven against a fake broker rather than a real one.

`FakeMqttClient` stands in for `aiomqtt.Client` -- the same shape
`test_hub_backends_ir.py`'s `FakeGateway` gives `pigpio` -- and is injected
through `MqttBridge`'s `client_factory` parameter, which exists specifically
so a test never has to open a real socket.

Everything else is real: a genuine `HubRuntime`, running against the
`virtual` backend the rest of the test suite already uses for exactly this
reason (`test_hub_runtime.py`), with a real `SceneEngine` behind it. The
bridge is the only thing being faked, because it is the only thing here that
would otherwise need a network.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from harmony_hub.bridge import bridge as bridge_impl
from harmony_hub.bridge.bridge import MqttBridge
from harmony_hub.bridge.topics import Topics
from harmony_hub.models import HubConfig
from harmony_hub.runtime import HubRuntime
from harmony_hub.settings import HubSettings

CONFIG = {
    "version": 1,
    "devices": [
        {"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": ["power_on", "power_off"]}}
    ],
    "scenes": [{"id": "watch_tv", "name": "Watch TV", "devices": ["tv"]}],
}
BUTTONS = {"power": {"label": "Power", "signatures": ["AA"]}}

TOPICS = Topics(node_id="harmony_hub")


# --------------------------------------------------------------------------
# Fake broker
# --------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeMqttClient:
    """Records every publish/subscribe; a test injects incoming messages with `inject`."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.subscriptions: list[str] = []
        self._incoming: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> "FakeMqttClient":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def publish(self, topic, payload, qos: int = 0, retain: bool = False) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.published.append((topic, payload, qos, retain))

    async def subscribe(self, topic, qos: int = 0) -> None:
        self.subscriptions.append(topic)

    @property
    def messages(self):
        return self._drain()

    async def _drain(self):
        while True:
            yield await self._incoming.get()

    def inject(self, topic: str, payload) -> None:
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._incoming.put_nowait(FakeMessage(topic, payload))

    def on_topic(self, topic: str) -> list[dict]:
        return [json.loads(p) for t, p, _, _ in self.published if t == topic]

    def count(self, topic: str) -> int:
        return sum(1 for t, *_ in self.published if t == topic)


class FailThenSucceed:
    """A `client_factory` that raises `attempts_before_success` times before handing out `fake`."""

    def __init__(self, fake: FakeMqttClient, attempts_before_success: int) -> None:
        self._fake = fake
        self._remaining = attempts_before_success
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise ConnectionRefusedError("broker refused the connection")
        return self._fake


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    async def _poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path):
    (tmp_path / "hub_config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    (tmp_path / "buttons.json").write_text(json.dumps(BUTTONS), encoding="utf-8")
    return tmp_path


async def _make_runtime(paths, **overrides) -> HubRuntime:
    fields = {
        "config_path": paths / "hub_config.json",
        "buttons_path": paths / "buttons.json",
        "mqtt_enabled": True,
        "mqtt_host": "broker.local",
        **overrides,
    }
    settings = HubSettings(**fields)
    runtime = HubRuntime(settings, settings_path=paths / "hub_settings.json")
    assert await runtime.start()
    return runtime


@pytest.fixture
async def env(paths):
    """A started hub, a bridge wired to a fake broker, and the fake itself -- already connected."""
    runtime = await _make_runtime(paths)
    fake = FakeMqttClient()
    bridge = MqttBridge(runtime, client_factory=lambda *a, **k: fake)
    runtime.mqtt_bridge = bridge

    bridge.start()
    await _wait_until(lambda: bridge.connected)
    await _wait_until(lambda: fake.count(TOPICS.discovery) >= 1 and fake.count(TOPICS.state) >= 1)

    yield runtime, bridge, fake

    await bridge.stop()
    await runtime.stop()


# --------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------


async def test_connecting_subscribes_to_the_command_wildcard(env):
    _runtime, _bridge, fake = env
    assert fake.subscriptions == [TOPICS.cmd_wildcard]


async def test_connecting_announces_online_with_retain(env):
    _runtime, _bridge, fake = env
    online = [(p, retain) for t, p, qos, retain in fake.published if t == TOPICS.availability]
    assert online[0] == (b"online", True)


async def test_discovery_reflects_the_configured_scene_and_device(env):
    _runtime, _bridge, fake = env
    doc = fake.on_topic(TOPICS.discovery)[-1]
    assert "scene_watch_tv" in doc["components"]
    assert "device_tv_available" in doc["components"]


async def test_bridge_reports_itself_connected(env):
    _runtime, bridge, _fake = env
    assert bridge.connected is True
    assert "broker.local" in bridge.detail


# --------------------------------------------------------------------------
# Commands: activity / scene
# --------------------------------------------------------------------------


async def test_activity_command_activates_a_scene(env):
    runtime, _bridge, fake = env
    fake.inject(TOPICS.cmd_activity, "watch_tv")
    await _wait_until(lambda: runtime.service.engine.active_scene == "watch_tv")


async def test_activity_command_off_stops_the_active_scene(env):
    runtime, _bridge, fake = env
    fake.inject(TOPICS.cmd_activity, "watch_tv")
    await _wait_until(lambda: runtime.service.engine.active_scene == "watch_tv")
    fake.inject(TOPICS.cmd_activity, "off")
    await _wait_until(lambda: runtime.service.engine.active_scene is None)


async def test_scene_command_topic_also_activates(env):
    runtime, _bridge, fake = env
    fake.inject(TOPICS.cmd_scene("watch_tv"), "ON")
    await _wait_until(lambda: runtime.service.engine.active_scene == "watch_tv")


async def test_an_unknown_scene_is_reported_rather_than_crashing_the_bridge(env):
    runtime, bridge, fake = env
    fake.inject(TOPICS.cmd_activity, "no_such_scene")
    await _wait_until(
        lambda: any(
            e.type == "status" and e.ok is False and "no_such_scene" in (e.detail or "")
            for e in runtime.broker.history
        )
    )
    # And the bridge itself is still alive and answering other commands.
    fake.inject(TOPICS.cmd_activity, "watch_tv")
    await _wait_until(lambda: runtime.service.engine.active_scene == "watch_tv")


# --------------------------------------------------------------------------
# Commands: paused / running / send
# --------------------------------------------------------------------------


async def test_paused_command_toggles_the_engine(env):
    runtime, _bridge, fake = env
    fake.inject(TOPICS.cmd_paused, "ON")
    await _wait_until(lambda: runtime.service.engine.paused is True)
    fake.inject(TOPICS.cmd_paused, "OFF")
    await _wait_until(lambda: runtime.service.engine.paused is False)


async def test_running_command_stops_and_restarts_the_hub(env):
    runtime, _bridge, fake = env
    fake.inject(TOPICS.cmd_running, "OFF")
    await _wait_until(lambda: runtime.service is None)
    fake.inject(TOPICS.cmd_running, "ON")
    await _wait_until(lambda: runtime.service is not None)


async def test_send_command_dispatches_to_the_named_backend(env):
    runtime, _bridge, fake = env
    fake.inject(TOPICS.cmd_send, {"device": "tv", "command": "power_on"})

    def _sent() -> bool:
        backend = runtime.service.engine.backend_for("tv")
        return any(c["command"] == "power_on" for c in backend.calls)

    await _wait_until(_sent)


async def test_send_command_with_invalid_json_is_reported_not_raised(env):
    runtime, _bridge, fake = env
    fake.inject(TOPICS.cmd_send, "{ not json")
    await _wait_until(
        lambda: any(e.type == "status" and e.ok is False for e in runtime.broker.history)
    )


async def test_send_command_missing_a_device_is_reported(env):
    runtime, _bridge, fake = env
    fake.inject(TOPICS.cmd_send, {"command": "power_on"})
    await _wait_until(
        lambda: any(e.type == "status" and e.ok is False for e in runtime.broker.history)
    )


# --------------------------------------------------------------------------
# Button events
# --------------------------------------------------------------------------


async def test_a_button_press_is_published_as_an_event(env):
    from harmony_hub.events import HubEvent

    runtime, _bridge, fake = env
    runtime.broker.publish(HubEvent(type="button", button="power", label="Power", phase="press", scene=None))
    await _wait_until(lambda: fake.count(TOPICS.button_event) >= 1)
    doc = fake.on_topic(TOPICS.button_event)[-1]
    assert doc == {"event_type": "power", "label": "Power", "phase": "press", "scene": None}


async def test_repeat_phase_is_not_published_by_default(env):
    from harmony_hub.events import HubEvent

    runtime, _bridge, fake = env
    before = fake.count(TOPICS.button_event)
    runtime.broker.publish(HubEvent(type="button", button="power", label="Power", phase="repeat", scene=None))
    # Give the listener a moment to (not) act -- there is nothing to wait
    # *for* here, so a short real sleep is the only option.
    await asyncio.sleep(0.05)
    assert fake.count(TOPICS.button_event) == before


async def test_repeat_phase_is_published_when_enabled(paths):
    from harmony_hub.events import HubEvent

    runtime = await _make_runtime(paths, mqtt_publish_repeats=True)
    fake = FakeMqttClient()
    bridge = MqttBridge(runtime, client_factory=lambda *a, **k: fake)
    runtime.mqtt_bridge = bridge
    bridge.start()
    await _wait_until(lambda: bridge.connected)

    runtime.broker.publish(HubEvent(type="button", button="power", label="Power", phase="repeat", scene=None))
    await _wait_until(lambda: fake.count(TOPICS.button_event) >= 1)

    await bridge.stop()
    await runtime.stop()


# --------------------------------------------------------------------------
# Discovery: removal across a config change
# --------------------------------------------------------------------------


async def test_deleting_a_scene_removes_its_component_in_two_steps(env):
    runtime, _bridge, fake = env
    before = fake.count(TOPICS.discovery)

    new_config = HubConfig(devices=runtime.config.devices, scenes=[])
    await runtime.save_config(new_config)

    await _wait_until(lambda: fake.count(TOPICS.discovery) >= before + 2)

    published = fake.on_topic(TOPICS.discovery)
    interim, final = published[-2], published[-1]
    assert interim["components"]["scene_watch_tv"] == {"platform": "scene"}
    assert "scene_watch_tv" not in final["components"]


async def test_republish_forces_a_publish_even_when_nothing_changed(env):
    _runtime, bridge, fake = env
    before = fake.count(TOPICS.discovery)
    assert await bridge.republish() is True
    assert fake.count(TOPICS.discovery) == before + 1


async def test_republish_returns_false_with_no_live_connection(paths):
    runtime = await _make_runtime(paths)
    bridge = MqttBridge(runtime, client_factory=lambda *a, **k: FakeMqttClient())
    # Deliberately never started.
    assert await bridge.republish() is False
    await runtime.stop()


# --------------------------------------------------------------------------
# Configuration edge cases
# --------------------------------------------------------------------------


async def test_a_disabled_bridge_never_connects(paths):
    runtime = await _make_runtime(paths, mqtt_enabled=False)
    fake = FakeMqttClient()
    bridge = MqttBridge(runtime, client_factory=lambda *a, **k: fake)
    bridge.start()
    await asyncio.sleep(0.05)
    assert bridge.connected is False
    assert bridge.detail == "disabled"
    assert fake.published == []
    await runtime.stop()


async def test_a_missing_broker_host_is_reported_without_crashing(paths):
    runtime = await _make_runtime(paths, mqtt_host="")
    fake = FakeMqttClient()
    bridge = MqttBridge(runtime, client_factory=lambda *a, **k: fake)
    bridge.start()
    await _wait_until(lambda: "no broker host" in bridge.detail)
    assert bridge.connected is False
    await runtime.stop()


async def test_reconnects_after_a_failed_first_attempt(paths, monkeypatch):
    monkeypatch.setattr(bridge_impl, "INITIAL_BACKOFF", 0.01)
    monkeypatch.setattr(bridge_impl, "MAX_BACKOFF", 0.02)

    runtime = await _make_runtime(paths)
    fake = FakeMqttClient()
    factory = FailThenSucceed(fake, attempts_before_success=2)
    bridge = MqttBridge(runtime, client_factory=factory)

    bridge.start()
    await _wait_until(lambda: bridge.connected, timeout=2.0)

    assert factory.calls == 3
    await bridge.stop()
    await runtime.stop()
