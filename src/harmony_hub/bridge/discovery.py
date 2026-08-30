"""Builds the Home Assistant MQTT discovery payload and the hub's state document.

Pure data in, data out -- no I/O, no MQTT client -- so the entity list a
given configuration produces can be checked in a test without a broker, the
same discipline `models.py` keeps for the same reason. The one function here
that is not purely descriptive is `diff_removed`, and even that is a plain
dict comparison; the actual two-step publish it drives lives in `bridge.py`.

**Device-based discovery**, one config topic carrying a `components` dict,
is what this uses rather than one config topic per entity: the whole hub
arrives in Home Assistant as a single device, and removing a component
(a deleted scene, say) is one dict key going missing rather than a retained
message on some other topic needing to be found and cleared.

**A hub scene becomes two things**, not one: a `select` option (so the
whole remote's current activity is one entity, matching how the physical
remote itself works -- one thing is active at a time) and its own `scene`
entity (so it can be activated directly from a dashboard tile or a
`scene.turn_on` call, the way any other Home Assistant scene can). Both
point at the same `cmd/scene/<id>` topic underneath.

**Command topics carry no schema of their own** beyond what `bridge.py`'s
handler expects -- `select`'s command payload is the chosen option verbatim,
`switch`'s is `ON`/`OFF`, `scene`'s is whatever Home Assistant's scene
platform sends on activation (ignored; the topic alone means "activate").
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from harmony_receiver.profiles import ButtonMap

from ..models import HubConfig
from .topics import Topics

ORIGIN = {
    "name": "Harmony Hub Replacement",
    "url": "https://github.com/windowslucker1121/HarmonyHubReplacement",
}

OFF_OPTION = "off"


def device_info(topics: Topics, name: str, sw_version: str, configuration_url: str = "") -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "identifiers": [f"harmony_hub_{topics.node_id}"],
        "name": name,
        "manufacturer": "Harmony Hub Replacement",
    }
    if sw_version:
        info["sw_version"] = sw_version
    if configuration_url:
        info["configuration_url"] = configuration_url
    return info


def _scene_icon(icon: Optional[str]) -> str:
    return f"mdi:{icon}" if icon else "mdi:remote-tv"


def _activity_component(topics: Topics, config: HubConfig) -> Dict[str, Any]:
    return {
        "platform": "select",
        "name": "Activity",
        "unique_id": topics.unique_id("activity"),
        "state_topic": topics.state,
        "value_template": "{{ value_json.active_scene or '" + OFF_OPTION + "' }}",
        "command_topic": topics.cmd_activity,
        "options": [OFF_OPTION] + [scene.id for scene in config.scenes],
        "icon": "mdi:remote-tv",
    }


def _scene_component(topics: Topics, scene) -> Dict[str, Any]:
    return {
        "platform": "scene",
        "name": scene.name,
        "unique_id": topics.unique_id(f"scene_{scene.id}"),
        "command_topic": topics.cmd_scene(scene.id),
        "icon": _scene_icon(scene.icon),
    }


def _button_event_component(topics: Topics, buttons: ButtonMap) -> Dict[str, Any]:
    # At least one entry is required by Home Assistant's `event` platform,
    # and "press" is always a legitimate phase even on a hub that has not
    # learned a single button yet.
    event_types = sorted({profile.key for profile in buttons}) or ["press"]
    return {
        "platform": "event",
        "name": "Remote button",
        "unique_id": topics.unique_id("button_event"),
        "state_topic": topics.button_event,
        "event_types": event_types,
        "icon": "mdi:gesture-tap-button",
    }


def _switch_component(topics: Topics, suffix: str, name: str, command_topic: str, icon: str) -> Dict[str, Any]:
    return {
        "platform": "switch",
        "name": name,
        "unique_id": topics.unique_id(suffix),
        "state_topic": topics.state,
        "value_template": "{{ 'ON' if value_json." + suffix + " else 'OFF' }}",
        "command_topic": command_topic,
        "icon": icon,
        "entity_category": "config",
    }


def _status_sensor(topics: Topics) -> Dict[str, Any]:
    return {
        "platform": "sensor",
        "name": "Status",
        "unique_id": topics.unique_id("status"),
        "state_topic": topics.state,
        "value_template": "{{ value_json.state }}",
        "json_attributes_topic": topics.state,
        "json_attributes_template": "{{ {'detail': value_json.detail} | tojson }}",
        "icon": "mdi:information-outline",
        "entity_category": "diagnostic",
    }


def _focus_sensor(topics: Topics) -> Dict[str, Any]:
    return {
        "platform": "sensor",
        "name": "Focus",
        "unique_id": topics.unique_id("focus"),
        "state_topic": topics.state,
        "value_template": "{{ value_json.focus_label or 'none' }}",
        "icon": "mdi:crosshairs-gps",
        "entity_category": "diagnostic",
    }


def _device_available_component(topics: Topics, device) -> Dict[str, Any]:
    return {
        "platform": "binary_sensor",
        "name": f"{device.name} available",
        "unique_id": topics.unique_id(f"device_{device.id}_available"),
        "state_topic": topics.state,
        "value_template": (
            "{{ 'ON' if (value_json.devices.get('" + device.id + "') or {}).get('ok') else 'OFF' }}"
        ),
        "device_class": "connectivity",
        "entity_category": "diagnostic",
    }


def components(topics: Topics, config: HubConfig, buttons: ButtonMap) -> Dict[str, Dict[str, Any]]:
    """Every entity this hub currently offers, keyed by a stable component id.

    The keys are what `diff_removed` compares between publishes, so they
    have to be derived only from things that survive a save unchanged --
    a scene or device's `id`, never its `name`, which the UI lets you edit
    freely without that meaning "this is now a different entity".
    """
    cmps: Dict[str, Dict[str, Any]] = {
        "activity": _activity_component(topics, config),
        "button_event": _button_event_component(topics, buttons),
        "paused": _switch_component(topics, "paused", "Paused", topics.cmd_paused, "mdi:pause-circle"),
        "running": _switch_component(topics, "running", "Hub running", topics.cmd_running, "mdi:power"),
        "status": _status_sensor(topics),
        "focus": _focus_sensor(topics),
    }
    for scene in config.scenes:
        cmps[f"scene_{scene.id}"] = _scene_component(topics, scene)
    for device in config.devices:
        cmps[f"device_{device.id}_available"] = _device_available_component(topics, device)
    return cmps


def payload(
    topics: Topics,
    config: HubConfig,
    buttons: ButtonMap,
    *,
    device_name: str,
    sw_version: str = "",
    configuration_url: str = "",
) -> Dict[str, Any]:
    """The full device-discovery document for this hub, as it is configured right now."""
    return {
        "device": device_info(topics, device_name, sw_version, configuration_url),
        "origin": ORIGIN,
        "availability_topic": topics.availability,
        "qos": 1,
        "components": components(topics, config, buttons),
    }


def diff_removed(previous: Dict[str, str], current: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Component keys `previous` knew about that `current` no longer has, with their platform.

    `previous` comes from `state_file.load_components` (what the last
    publish told Home Assistant); `current` is this publish's `components`.
    Home Assistant does not infer a removal from a key simply being absent
    from a republished device config -- see the module docstring on why the
    two-step dance in `bridge.py` needs exactly this to know what to
    announce as gone.
    """
    return {key: platform for key, platform in previous.items() if key not in current}


def fingerprint(doc: Dict[str, Any]) -> str:
    """A stable string for "has the discovery document actually changed".

    Compared between publishes so an unrelated hub event (a button press,
    a scene switch) does not re-publish a retained discovery message that
    would tell Home Assistant nothing new -- `sort_keys` is what makes two
    builds of the same configuration compare equal regardless of dict
    ordering.
    """
    return json.dumps(doc, sort_keys=True)


def state(
    *,
    hub_state: str,
    hub_detail: str,
    active_scene: Optional[str],
    paused: bool,
    running: bool,
    focus_label: Optional[str],
    devices: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """The one retained JSON document every templated entity above reads from.

    One document rather than one topic per fact: every entity update lands
    atomically together, and adding a new templated entity later never
    means a new topic to also start publishing.
    """
    return {
        "state": hub_state,
        "detail": hub_detail,
        "active_scene": active_scene or None,
        "paused": paused,
        "running": running,
        "focus_label": focus_label,
        "devices": {
            str(d["id"]): {"ok": bool(d["ok"]), "running": bool(d["running"]), "detail": str(d.get("detail", ""))}
            for d in devices
        },
    }


def button_event(*, key: str, label: str, phase: str, scene: Optional[str]) -> Dict[str, Any]:
    """One payload for `topics.button_event` -- an HA `event` entity reads `event_type` and treats the rest as attributes."""
    return {"event_type": key, "label": label, "phase": phase, "scene": scene}
