"""Generic HTTP backend: each command is a declared request.

The catch-all for anything with a web API -- an ESP32 IR blaster, a TV with
a REST interface, a webhook into another automation system. Because commands
are declared in configuration rather than in code, reaching a new device
usually needs no Python at all.

It is also the reference case for the plugin interface: if something can be
expressed here, a purpose-built backend for it is an optimisation rather
than a necessity.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from . import Backend, BackendError, Command, Health, register

logger = logging.getLogger("HUB.http")

DEFAULT_TIMEOUT = 5.0


@register
class HttpBackend(Backend):
    """Sends a configured HTTP request per command."""

    name = "http"
    label = "HTTP / webhook"
    description = "Calls a declared HTTP request for each command."

    def __init__(self, device_id: str, config: Dict[str, Any]) -> None:
        super().__init__(device_id, config)
        self._client: Optional[httpx.AsyncClient] = None

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["commands"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "title": "Base URL",
                    "description": "Prefixed to each command's path, e.g. http://192.168.1.50",
                    "default": "",
                },
                "headers": {
                    "type": "object",
                    "title": "Headers",
                    "description": "Sent with every request, e.g. an Authorization header.",
                    "additionalProperties": {"type": "string"},
                    "default": {},
                },
                "verify_tls": {"type": "boolean", "title": "Verify TLS certificates", "default": True},
                "timeout": {"type": "number", "title": "Timeout (seconds)", "default": DEFAULT_TIMEOUT},
                "commands": {
                    "type": "object",
                    "title": "Commands",
                    "description": "Command name to request definition.",
                    "additionalProperties": {
                        "type": "object",
                        "required": ["path"],
                        "properties": {
                            "method": {"type": "string", "default": "POST"},
                            "path": {"type": "string"},
                            "json": {"type": "object"},
                            "data": {"type": "string"},
                            "label": {"type": "string"},
                        },
                    },
                    "default": {},
                },
            },
        }

    async def connect(self) -> None:
        await self.close()
        self._client = httpx.AsyncClient(
            base_url=self.config.get("base_url", ""),
            headers=self.config.get("headers") or {},
            timeout=self.config.get("timeout", DEFAULT_TIMEOUT),
            verify=self.config.get("verify_tls", True),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def commands(self) -> List[Command]:
        declared: Dict[str, Any] = self.config.get("commands") or {}
        return [
            Command(
                name=name,
                label=spec.get("label") or name.replace("_", " ").title(),
                description=f"{spec.get('method', 'POST')} {spec.get('path', '')}",
            )
            for name, spec in declared.items()
        ]

    async def send(self, command: str, params: Optional[Dict[str, Any]] = None) -> None:
        spec = (self.config.get("commands") or {}).get(command)
        if spec is None:
            raise BackendError(f"device '{self.device_id}' has no HTTP command '{command}'")
        if self._client is None:
            await self.connect()
        assert self._client is not None

        # Parameters substitute into the path so one declared command can
        # cover a family of them, e.g. /key/{key} driven from the binding.
        path = spec["path"].format(**(params or {}))
        try:
            response = await self._client.request(
                spec.get("method", "POST"), path, json=spec.get("json"), content=spec.get("data")
            )
            response.raise_for_status()
        except httpx.HTTPError as err:
            raise BackendError(f"{self.device_id}.{command} failed: {err}") from err

    async def health(self) -> Health:
        base_url = self.config.get("base_url")
        if not base_url:
            return Health(ok=True, detail="no base URL to check")
        if self._client is None:
            await self.connect()
        assert self._client is not None
        try:
            await self._client.get("/")
            return Health(ok=True, detail=f"reachable at {base_url}")
        except httpx.HTTPStatusError:
            # Any answer at all proves something is listening, which is the
            # only thing this check is trying to establish.
            return Health(ok=True, detail=f"reachable at {base_url}")
        except httpx.HTTPError as err:
            return Health(ok=False, detail=str(err))
