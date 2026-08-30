"""Builds the MQTT client connection for one bridge session.

Kept to one small function so `bridge.py` never imports `aiomqtt` directly --
`MqttBridge` takes a `client_factory` callable instead (see its docstring),
and a test supplies a fake one instead of this, the same way
`test_hub_backends_ir.py` supplies a fake `pigpio` rather than the real
module.
"""

from __future__ import annotations

import aiomqtt

from ..settings import HubSettings
from .topics import Topics


def build_client(settings: HubSettings, password: str, topics: Topics) -> aiomqtt.Client:
    """One `aiomqtt.Client`, configured with this hub's Last Will.

    The Will is what makes `binary_sensor`/availability in Home Assistant
    correct even when the hub process is killed rather than stopped
    cleanly -- the broker publishes it the moment the TCP connection drops,
    with no cooperation needed from a process that is no longer running.
    A clean shutdown additionally publishes "offline" itself (see
    `bridge.Bridge._session`'s `finally`), which is faster than waiting on
    the broker's keepalive timeout to notice the same thing.
    """
    will = aiomqtt.Will(topic=topics.availability, payload=b"offline", qos=1, retain=True)
    tls_params = aiomqtt.TLSParameters() if settings.mqtt_tls else None
    return aiomqtt.Client(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        username=settings.mqtt_username or None,
        password=password or None,
        identifier=f"harmony-hub-{topics.node_id}",
        will=will,
        tls_params=tls_params,
        timeout=10.0,
        keepalive=30,
    )
