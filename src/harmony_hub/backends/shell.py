"""Runs a pre-declared local command per button action.

The escape hatch for anything with a command-line tool and no API -- `adb`,
`irsend`, a vendor utility, a script of your own.

Commands are looked up in this device's configuration by name and never
built from anything the caller supplies. That is deliberate: the hub exposes
a web UI, and an action that could run arbitrary text would turn every
button binding into remote code execution on the machine running the hub.
Adding a command is a config change, which is a decision someone makes
once, rather than something a request can do.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any, Dict, List, Optional

from . import Backend, BackendError, Command, Health, register

logger = logging.getLogger("HUB.shell")

DEFAULT_TIMEOUT = 10.0


@register
class ShellBackend(Backend):
    """Executes a configured local program per command."""

    name = "shell"
    label = "Local command"
    description = "Runs a pre-declared program on the machine running the hub."

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["commands"],
            "properties": {
                "timeout": {"type": "number", "title": "Timeout (seconds)", "default": DEFAULT_TIMEOUT},
                "commands": {
                    "type": "object",
                    "title": "Commands",
                    "description": "Command name to the program and arguments to run.",
                    "additionalProperties": {
                        "oneOf": [
                            {"type": "string", "title": "Command line"},
                            {
                                "type": "object",
                                "required": ["argv"],
                                "properties": {
                                    "argv": {"type": "array", "items": {"type": "string"}},
                                    "label": {"type": "string"},
                                },
                            },
                        ]
                    },
                    "default": {},
                },
            },
        }

    def _argv(self, command: str) -> List[str]:
        spec = (self.config.get("commands") or {}).get(command)
        if spec is None:
            raise BackendError(f"device '{self.device_id}' has no shell command '{command}'")
        if isinstance(spec, str):
            # posix=False keeps Windows paths like C:\adb\adb.exe intact,
            # which shlex would otherwise read as escape sequences.
            return shlex.split(spec, posix=False)
        return list(spec["argv"])

    async def commands(self) -> List[Command]:
        declared: Dict[str, Any] = self.config.get("commands") or {}
        result = []
        for name, spec in declared.items():
            label = spec.get("label") if isinstance(spec, dict) else None
            described = spec if isinstance(spec, str) else " ".join(spec.get("argv", []))
            result.append(
                Command(name=name, label=label or name.replace("_", " ").title(), description=described)
            )
        return result

    async def send(self, command: str, params: Optional[Dict[str, Any]] = None) -> None:
        argv = self._argv(command)
        timeout = self.config.get("timeout", DEFAULT_TIMEOUT)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except OSError as err:
            raise BackendError(f"{self.device_id}.{command}: could not run {argv[0]!r}: {err}") from err

        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            raise BackendError(f"{self.device_id}.{command} timed out after {timeout}s")

        if process.returncode:
            detail = (stderr or b"").decode(errors="replace").strip()
            raise BackendError(f"{self.device_id}.{command} exited {process.returncode}: {detail}")

    async def health(self) -> Health:
        return Health(ok=True, detail=f"{len(self.config.get('commands') or {})} command(s) declared")
