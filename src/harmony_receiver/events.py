"""Typed events produced from Harmony remote RF frames."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

EventKind = Literal["press", "repeat", "release"]


@dataclass(frozen=True)
class RemoteEvent:
    """A single decoded button event from a Harmony remote.

    A button is identified by its HID `usage` on a given `report` page, which
    is what the remote actually transmits. `signature` (the raw hex of
    payload bytes 1..4) is kept alongside it so a button whose usage isn't in
    the HID tables can still be given a name by hand, via `profiles`.
    """

    kind: EventKind
    signature: str
    report: str = "unknown"  # "keyboard" or "consumer"
    usage: Optional[int] = None
    label: Optional[str] = None  # HID name, or a learned override
    raw: bytes = b""
    channel: Optional[int] = None
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def name(self) -> str:
        """The friendly button name, falling back to something identifying."""
        if self.label:
            return self.label
        if self.usage is not None:
            return f"{self.report} 0x{self.usage:02X}"
        return f"<{self.signature}>"

    def __str__(self) -> str:
        ts = self.timestamp.strftime("%H:%M:%S.%f")[:-3]
        return f"[{ts}] {self.kind.upper():<7} {self.name}"
