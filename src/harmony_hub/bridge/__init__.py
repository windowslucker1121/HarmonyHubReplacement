"""Publishes this hub into Home Assistant, over MQTT discovery.

`backends/homeassistant.py` is the hub pointing outward at a Home Assistant
instance -- one more device it can drive. This package is the other
direction: Home Assistant discovering the *hub itself* as a device, with an
activity selector, a scene per hub scene, an event for every remote button
press, and a couple of diagnostic entities -- so a Home Assistant automation
can react to a physical Harmony button, and so the six-year-old remote
becomes one more input Home Assistant already knows how to use.

`topics.py` lays out the MQTT topic tree. `discovery.py` and `state.py` are
pure functions -- configuration in, JSON documents out -- so the entity list
a given `HubConfig` produces can be checked without a broker, the same
discipline `models.py` keeps for the same reason. `credentials.py` keeps the
broker password out of `hub_settings.json`, the same way the Home Assistant
*backend* keeps its access token out of device configuration. `bridge.py` is
the only part of this package that touches a socket: the reconnecting MQTT
session, owned by `HubRuntime` so it outlives a hub restart the same way the
event broker does.
"""

from __future__ import annotations

from .bridge import MqttBridge

__all__ = ["MqttBridge"]
