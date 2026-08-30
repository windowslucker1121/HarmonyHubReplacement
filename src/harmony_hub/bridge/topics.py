"""MQTT topic layout for the Home Assistant bridge.

Every topic hangs off one root, `harmony_hub/<node_id>`, independent of
`discovery_prefix` (which only locates the *discovery* document -- Home
Assistant's own convention, and configurable separately because a shared
broker might already use `homeassistant/` for something else). One root
means one wildcard subscription, `harmony_hub/<node_id>/cmd/#`, catches
every command topic without enumerating them, and nothing here can collide
with another device's topics on the same broker.
"""

from __future__ import annotations

from dataclasses import dataclass

CMD_ACTIVITY = "activity"
CMD_PAUSED = "paused"
CMD_RUNNING = "running"
CMD_SEND = "send"
CMD_SCENE_PREFIX = "scene/"


@dataclass(frozen=True)
class Topics:
    node_id: str
    discovery_prefix: str = "homeassistant"

    @property
    def root(self) -> str:
        return f"harmony_hub/{self.node_id}"

    @property
    def availability(self) -> str:
        return f"{self.root}/availability"

    @property
    def state(self) -> str:
        return f"{self.root}/state"

    @property
    def button_event(self) -> str:
        return f"{self.root}/event/button"

    @property
    def cmd_root(self) -> str:
        return f"{self.root}/cmd/"

    @property
    def cmd_wildcard(self) -> str:
        return f"{self.root}/cmd/#"

    @property
    def cmd_activity(self) -> str:
        return f"{self.cmd_root}{CMD_ACTIVITY}"

    def cmd_scene(self, scene_id: str) -> str:
        return f"{self.cmd_root}{CMD_SCENE_PREFIX}{scene_id}"

    @property
    def cmd_paused(self) -> str:
        return f"{self.cmd_root}{CMD_PAUSED}"

    @property
    def cmd_running(self) -> str:
        return f"{self.cmd_root}{CMD_RUNNING}"

    @property
    def cmd_send(self) -> str:
        return f"{self.cmd_root}{CMD_SEND}"

    @property
    def discovery(self) -> str:
        return f"{self.discovery_prefix}/device/{self.node_id}/config"

    def unique_id(self, suffix: str) -> str:
        return f"{self.node_id}_{suffix}"
