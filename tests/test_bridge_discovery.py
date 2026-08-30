"""`bridge.discovery` and `bridge.topics`: pure builders, no broker involved.

Mirrors `test_hub_models.py`'s discipline of exercising the data shape
directly, since none of this touches I/O -- see `discovery.py`'s module
docstring for why that separation exists.
"""

from __future__ import annotations

from harmony_receiver.profiles import ButtonMap, ButtonProfile

from harmony_hub.bridge import discovery
from harmony_hub.bridge.topics import Topics
from harmony_hub.models import Device, HubConfig, Scene

TOPICS = Topics(node_id="harmony_hub")


def _config(**overrides) -> HubConfig:
    defaults = dict(
        devices=[Device(id="tv", name="Living Room TV", backend="lgtv")],
        scenes=[Scene(id="watch_tv", name="Watch TV", icon="television")],
    )
    defaults.update(overrides)
    return HubConfig(**defaults)


def _buttons() -> ButtonMap:
    return ButtonMap(
        {
            "power": ButtonProfile(key="power", label="Power", signatures={"AA"}),
            "volume_up": ButtonProfile(key="volume_up", label="Volume Up", signatures={"BB"}),
        }
    )


# --------------------------------------------------------------------------
# Topics
# --------------------------------------------------------------------------


def test_topics_hang_off_one_root():
    topics = Topics(node_id="hub1")
    assert topics.root == "harmony_hub/hub1"
    assert topics.availability == "harmony_hub/hub1/availability"
    assert topics.state == "harmony_hub/hub1/state"
    assert topics.cmd_activity == "harmony_hub/hub1/cmd/activity"
    assert topics.cmd_scene("watch_tv") == "harmony_hub/hub1/cmd/scene/watch_tv"
    assert topics.cmd_wildcard == "harmony_hub/hub1/cmd/#"
    assert topics.cmd_activity.startswith(topics.cmd_root)


def test_discovery_topic_uses_its_own_prefix():
    topics = Topics(node_id="hub1", discovery_prefix="custom")
    assert topics.discovery == "custom/device/hub1/config"


def test_unique_id_is_namespaced_by_node():
    topics = Topics(node_id="hub1")
    assert topics.unique_id("activity") == "hub1_activity"


# --------------------------------------------------------------------------
# Discovery payload
# --------------------------------------------------------------------------


def test_payload_has_one_component_per_scene_and_device():
    doc = discovery.payload(TOPICS, _config(), _buttons(), device_name="Harmony Hub")
    cmps = doc["components"]
    assert "scene_watch_tv" in cmps
    assert cmps["scene_watch_tv"]["platform"] == "scene"
    assert "device_tv_available" in cmps
    assert cmps["device_tv_available"]["platform"] == "binary_sensor"


def test_payload_always_has_the_fixed_entities():
    doc = discovery.payload(TOPICS, _config(), _buttons(), device_name="Harmony Hub")
    cmps = doc["components"]
    for key, platform in [
        ("activity", "select"),
        ("button_event", "event"),
        ("paused", "switch"),
        ("running", "switch"),
        ("status", "sensor"),
        ("focus", "sensor"),
    ]:
        assert cmps[key]["platform"] == platform


def test_activity_options_are_off_plus_every_scene_id():
    config = _config(scenes=[Scene(id="a", name="A"), Scene(id="b", name="B")])
    doc = discovery.payload(TOPICS, config, _buttons(), device_name="Harmony Hub")
    assert doc["components"]["activity"]["options"] == ["off", "a", "b"]


def test_button_event_types_come_from_the_button_map():
    doc = discovery.payload(TOPICS, _config(), _buttons(), device_name="Harmony Hub")
    assert doc["components"]["button_event"]["event_types"] == ["power", "volume_up"]


def test_button_event_types_fall_back_when_nothing_is_learned_yet():
    doc = discovery.payload(TOPICS, _config(), ButtonMap(), device_name="Harmony Hub")
    assert doc["components"]["button_event"]["event_types"] == ["press"]


def test_unique_ids_are_stable_across_two_builds_of_the_same_config():
    a = discovery.payload(TOPICS, _config(), _buttons(), device_name="Harmony Hub")
    b = discovery.payload(TOPICS, _config(), _buttons(), device_name="Harmony Hub")
    ids_a = {c["unique_id"] for c in a["components"].values()}
    ids_b = {c["unique_id"] for c in b["components"].values()}
    assert ids_a == ids_b
    assert len(ids_a) == len(a["components"])  # no collisions


def test_renaming_a_scene_does_not_change_its_component_key():
    """A component's key must survive a rename, or `diff_removed` would think it was deleted."""
    before = discovery.payload(TOPICS, _config(), _buttons(), device_name="Harmony Hub")
    renamed = _config(scenes=[Scene(id="watch_tv", name="Movie Night")])
    after = discovery.payload(TOPICS, renamed, _buttons(), device_name="Harmony Hub")
    assert set(before["components"]) == set(after["components"])
    assert after["components"]["scene_watch_tv"]["name"] == "Movie Night"


def test_availability_and_device_identity():
    doc = discovery.payload(TOPICS, _config(), _buttons(), device_name="My Hub", sw_version="1.2.3")
    assert doc["availability_topic"] == TOPICS.availability
    assert doc["device"]["identifiers"] == ["harmony_hub_harmony_hub"]
    assert doc["device"]["name"] == "My Hub"
    assert doc["device"]["sw_version"] == "1.2.3"


def test_configuration_url_omitted_when_blank():
    doc = discovery.payload(TOPICS, _config(), _buttons(), device_name="Hub")
    assert "configuration_url" not in doc["device"]


# --------------------------------------------------------------------------
# Removal diffing
# --------------------------------------------------------------------------


def test_diff_removed_finds_only_what_disappeared():
    previous = {"scene_a": "scene", "scene_b": "scene", "activity": "select"}
    current = {"scene_a": {"platform": "scene"}, "activity": {"platform": "select"}}
    assert discovery.diff_removed(previous, current) == {"scene_b": "scene"}


def test_diff_removed_is_empty_when_nothing_disappeared():
    previous = {"activity": "select"}
    current = {"activity": {"platform": "select"}, "scene_a": {"platform": "scene"}}
    assert discovery.diff_removed(previous, current) == {}


def test_deleting_a_scene_removes_exactly_its_component():
    config = _config(scenes=[Scene(id="a", name="A"), Scene(id="b", name="B")])
    before = discovery.payload(TOPICS, config, _buttons(), device_name="Hub")["components"]

    after_delete = _config(scenes=[Scene(id="a", name="A")])
    after = discovery.payload(TOPICS, after_delete, _buttons(), device_name="Hub")["components"]

    previous_map = {key: comp["platform"] for key, comp in before.items()}
    removed = discovery.diff_removed(previous_map, after)
    assert removed == {"scene_b": "scene"}


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------


def test_fingerprint_is_stable_regardless_of_dict_build_order():
    doc = discovery.payload(TOPICS, _config(), _buttons(), device_name="Hub")
    reordered = {k: doc[k] for k in reversed(list(doc))}
    assert discovery.fingerprint(doc) == discovery.fingerprint(reordered)


def test_fingerprint_changes_when_a_scene_is_added():
    a = discovery.payload(TOPICS, _config(), _buttons(), device_name="Hub")
    b = discovery.payload(
        TOPICS, _config(scenes=[Scene(id="watch_tv", name="Watch TV"), Scene(id="movie", name="Movie")]),
        _buttons(), device_name="Hub",
    )
    assert discovery.fingerprint(a) != discovery.fingerprint(b)


# --------------------------------------------------------------------------
# State and button-event documents
# --------------------------------------------------------------------------


def test_state_document_shape():
    doc = discovery.state(
        hub_state="running", hub_detail="ok", active_scene="watch_tv", paused=False, running=True,
        focus_label="Living room lamp",
        devices=[{"id": "tv", "ok": True, "running": True, "detail": ""}],
    )
    assert doc["active_scene"] == "watch_tv"
    assert doc["devices"]["tv"] == {"ok": True, "running": True, "detail": ""}


def test_state_document_uses_none_for_no_active_scene():
    doc = discovery.state(
        hub_state="stopped", hub_detail="", active_scene=None, paused=False, running=False,
        focus_label=None, devices=[],
    )
    assert doc["active_scene"] is None
    assert doc["focus_label"] is None
    assert doc["devices"] == {}


def test_button_event_document():
    doc = discovery.button_event(key="power", label="Power", phase="press", scene="watch_tv")
    assert doc == {"event_type": "power", "label": "Power", "phase": "press", "scene": "watch_tv"}
