"""A backend that does nothing but remember what it was told.

This exists so the whole platform -- scenes, bindings, the editor, the live
view -- can be built and demonstrated before a single piece of real
equipment is wired up, and so the engine's tests never need hardware or a
network. Every command it is given is recorded and can be asserted on.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from . import Backend, BackendError, Command, FocusTarget, Health, register

logger = logging.getLogger("HUB.virtual")

DEFAULT_COMMANDS = ["power_on", "power_off", "volume_up", "volume_down", "mute"]


@register
class VirtualBackend(Backend):
    """Records commands instead of sending them. For testing and UI work.

    Unknown commands are rejected rather than silently accepted. Real
    equipment would reject them too, and a stub that swallows a typo would
    make a scene look like it works right up until it is pointed at
    hardware.
    """

    name = "virtual"
    label = "Virtual device"
    description = "Pretends to be equipment. Records every command instead of sending it."

    def __init__(self, device_id: str, config: Dict[str, Any]) -> None:
        super().__init__(device_id, config)
        self.calls: List[Dict[str, Any]] = []

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "title": "Commands",
                    "description": "Command names this pretend device should offer.",
                    "default": DEFAULT_COMMANDS,
                },
                "focus": {
                    "type": "object",
                    "title": "Focus map",
                    "description": "command -> [target, label], for testing the SmartHome +/- keys "
                    "without real equipment.",
                    "default": {},
                },
                "adjust": {
                    "type": "object",
                    "title": "Adjust map",
                    "description": "target -> {up: command, down: command}, for testing the "
                    "SmartHome +/- keys without real equipment.",
                    "default": {},
                },
            },
        }

    @property
    def _names(self) -> List[str]:
        return self.config.get("commands") or DEFAULT_COMMANDS

    async def commands(self) -> List[Command]:
        return [Command(name=n, label=n.replace("_", " ").title()) for n in self._names]

    async def send(self, command: str, params: Optional[Dict[str, Any]] = None) -> None:
        if command not in self._names:
            raise BackendError(
                f"device '{self.device_id}' has no command '{command}' "
                f"(offers: {', '.join(self._names)})"
            )
        self.calls.append({"command": command, "params": params or {}, "at": time.monotonic()})
        logger.info("[virtual:%s] %s %s", self.device_id, command, params or "")

    async def health(self) -> Health:
        return Health(ok=True, detail=f"{len(self.calls)} command(s) recorded")

    def focus_for(self, command: str) -> Optional[FocusTarget]:
        """Driven entirely by `config["focus"]`, so the engine's focus
        tracking can be tested without a backend that means anything."""
        entry = (self.config.get("focus") or {}).get(command)
        if entry is None:
            return None
        target, label = (entry, entry) if isinstance(entry, str) else (entry[0], entry[1])
        return FocusTarget(target=target, label=label)

    def adjust_command(self, target: str, direction: str) -> Optional[str]:
        """Driven entirely by `config["adjust"]`; see `focus_for`."""
        return (self.config.get("adjust") or {}).get(target, {}).get(direction)
