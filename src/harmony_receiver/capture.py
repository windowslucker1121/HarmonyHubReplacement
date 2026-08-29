"""Append-only JSONL logging of every raw packet seen.

Reverse engineering happens after the fact, by comparing captures across
sessions -- which a scrolling console cannot support. One JSON object per
packet keeps the log greppable, diffable, and loadable straight into an
analysis script.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Optional, Type


class CaptureLog:
    """Writes one JSON object per received packet, flushed as it goes.

    Flushing on every record costs little at Harmony's packet rates and means
    a session killed with Ctrl+C still leaves a complete log behind.
    """

    def __init__(self, path: str | Path, note: str = "") -> None:
        self.path = Path(path)
        self._note = note
        self._file = None
        self._start = 0.0

    def __enter__(self) -> "CaptureLog":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self._start = time.monotonic()
        self._write({"type": "session", "started": datetime.now().isoformat(), "note": self._note})
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _write(self, record: dict) -> None:
        if self._file is None:
            return
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()

    def packet(self, payload: bytes, pipe: Optional[int], channel: Optional[int], kind: Optional[str]) -> None:
        self._write(
            {
                "type": "packet",
                "t": round(time.monotonic() - self._start, 4),
                "channel": channel,
                "pipe": pipe,
                "len": len(payload),
                "kind": kind,
                "raw": payload.hex().upper(),
            }
        )

    def mark(self, label: str, **extra: object) -> None:
        """Records a non-packet milestone, e.g. a channel lock or a learn prompt."""
        self._write({"type": "mark", "t": round(time.monotonic() - self._start, 4), "label": label, **extra})
