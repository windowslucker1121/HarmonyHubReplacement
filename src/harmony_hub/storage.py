"""Crash-safe JSON writing, shared by everything the hub persists.

One implementation rather than one per file, because the guarantee is subtle
enough that a second copy would eventually drift from this one.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("HUB.storage")


def write_json(payload: Any, path: str | Path) -> None:
    """Writes JSON atomically, leaving the previous file intact on any failure.

    Written to a temporary file in the same directory and then renamed, so a
    crash or a full disk mid-write leaves the previous contents rather than a
    truncated file nothing can start from. Same directory matters:
    `os.replace` is only atomic within a filesystem.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
