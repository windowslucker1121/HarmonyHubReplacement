"""The IR learn routes, driven end to end through a real hub with a fake gateway.

Uses `TestClient` against a real `create_app()`, the same way `test_hub_api.py`
does, so this covers routing and serialisation for the six `/learn` routes
rather than just the backend methods underneath them (already covered in
`test_hub_backends_ir.py`).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from harmony_hub.api import create_app
from harmony_hub.ir import learn as learn_module
from harmony_hub.ir.gateway import IrTimeout
from harmony_hub.service import HubSettings

CONFIG = {
    "version": 1,
    "devices": [
        {"id": "tv", "name": "TV", "backend": "ir", "config": {}},
        {"id": "soundbar", "name": "Soundbar", "backend": "ir", "config": {}},
        {"id": "shield", "name": "Shield", "backend": "virtual", "config": {}},
    ],
}

BUTTONS = {"volume_up": {"label": "Volume Up", "signatures": ["C3E90000"]}}


class FakeGateway:
    def __init__(self, rx_ready: bool = True, tx_ready: bool = True) -> None:
        self.rx_ready = rx_ready
        self.tx_ready = tx_ready
        self.rx_configured = rx_ready
        self.tx_configured = tx_ready
        self.sent: list = []
        self._captures: list = []

    def shutdown(self) -> None:
        pass

    def queue_capture(self, result) -> None:
        self._captures.append(result)

    async def capture(self, timeout: float, gap_us: int = 10_000):
        if not self._captures:
            # Nothing queued -- block rather than resolving instantly, so a
            # test can observe the job as still genuinely in flight (busy)
            # instead of racing its own instant timeout. Cleanly cancelled
            # by `learn_cancel` or by the test client's own teardown.
            await asyncio.sleep(3600)
            raise IrTimeout("no signal was received")
        result = self._captures.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def transmit(self, timings, carrier_hz, *, duty_cycle=0.33, repeats=1, gap_ms=40.0) -> None:
        self.sent.append(list(timings))

    def health(self):
        return True, "receive GPIO17, transmit GPIO18"


@pytest.fixture(autouse=True)
def _reset_shared_learn_job():
    learn_module.job()._reset()
    yield
    learn_module.job()._reset()


@pytest.fixture
def fake_gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr("harmony_hub.backends.ir.ir_gateway.gateway", lambda: fake)
    return fake


@pytest.fixture
def client(tmp_path, fake_gateway):
    config = json.loads(json.dumps(CONFIG))
    for device in config["devices"]:
        if device["backend"] == "ir":
            device["config"]["codes_dir"] = str(tmp_path / "codes" / device["id"])

    config_path = tmp_path / "hub_config.json"
    buttons_path = tmp_path / "buttons.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    buttons_path.write_text(json.dumps(BUTTONS), encoding="utf-8")

    app = create_app(
        HubSettings(config_path=config_path, buttons_path=buttons_path),
        settings_path=tmp_path / "hub_settings.json",
    )
    with TestClient(app) as client:
        client.fake_gateway = fake_gateway
        yield client


def test_backends_say_whether_they_can_learn():
    """No hub needed -- `/api/backends` reflects the class, not any instance."""
    app = create_app(HubSettings())
    with TestClient(app) as client:
        names = {b["name"]: b for b in client.get("/api/backends").json()}
    assert names["ir"]["learnable"] is True
    assert names["ir"]["learn_label"]
    assert names["ir"]["learn_hint"]
    assert names["virtual"]["learnable"] is False


def test_a_non_learnable_device_refuses_with_400(client):
    response = client.post("/api/devices/shield/learn/start")
    assert response.status_code == 400


def test_an_unknown_device_is_404(client):
    assert client.get("/api/devices/nonexistent/learn").status_code == 404


def test_the_full_learn_and_bind_flow(client):
    client.fake_gateway.queue_capture([9000, 4500, 560, 1690])
    client.fake_gateway.queue_capture([9012, 4488, 561, 1688])

    started = client.post("/api/devices/tv/learn/start").json()
    assert started["state"] == "waiting"

    for _ in range(200):
        status = client.get("/api/devices/tv/learn").json()
        if status["state"] not in ("waiting", "confirming"):
            break
    assert status["state"] == "captured"
    assert status["pulses"] == 4

    verify = client.post("/api/devices/tv/learn/verify").json()
    assert verify["ok"] is True
    assert client.fake_gateway.sent == [[9000, 4500, 560, 1690]]

    commands = client.post(
        "/api/devices/tv/learn/save",
        json={"name": "volume_up", "label": "Volume Up", "repeatable": True, "repeats": 1},
    ).json()
    assert [c["name"] for c in commands] == ["volume_up"]
    assert commands[0]["repeatable"] is True

    # And it shows up through the ordinary commands route too, exactly like
    # any other backend's -- binding a button does not need to know this
    # command came from learning rather than a fixed table.
    ordinary = client.get("/api/devices/tv/commands").json()
    assert [c["name"] for c in ordinary] == ["volume_up"]

    suggested = client.get("/api/devices/tv/suggested_bindings").json()
    assert suggested["bindings"] == {"volume_up": "volume_up"}


def test_a_second_device_learning_is_refused_with_409(client):
    client.post("/api/devices/tv/learn/start")

    response = client.post("/api/devices/soundbar/learn/start")

    assert response.status_code == 409
    assert "tv" in response.json()["detail"]

    client.post("/api/devices/tv/learn/cancel")  # release the still-hanging capture


def test_cancel_frees_the_receiver_for_another_device(client):
    client.post("/api/devices/tv/learn/start")

    cancelled = client.post("/api/devices/tv/learn/cancel").json()
    assert cancelled["state"] == "idle"

    started = client.post("/api/devices/soundbar/learn/start").json()
    assert started["state"] == "waiting"


def test_forgetting_a_learned_command_removes_it(client):
    client.fake_gateway.queue_capture([1000, 2000, 1000, 2000])
    client.fake_gateway.queue_capture([1000, 2000, 1000, 2000])
    client.post("/api/devices/tv/learn/start")
    for _ in range(200):
        if client.get("/api/devices/tv/learn").json()["state"] == "captured":
            break
    client.post("/api/devices/tv/learn/save", json={"name": "mute", "label": "Mute"})

    remaining = client.delete("/api/devices/tv/learn/mute").json()

    assert remaining == []


def test_saving_with_an_invalid_name_is_rejected(client):
    client.fake_gateway.queue_capture([1000, 2000, 1000, 2000])
    client.fake_gateway.queue_capture([1000, 2000, 1000, 2000])
    client.post("/api/devices/tv/learn/start")
    for _ in range(200):
        if client.get("/api/devices/tv/learn").json()["state"] == "captured":
            break

    response = client.post(
        "/api/devices/tv/learn/save", json={"name": "Volume Up!", "label": "Volume Up"}
    )

    assert response.status_code == 422


def test_saving_with_nothing_captured_yet_is_a_conflict(client):
    response = client.post(
        "/api/devices/tv/learn/save", json={"name": "volume_up", "label": "Volume Up"}
    )
    assert response.status_code == 409
