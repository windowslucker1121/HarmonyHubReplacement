"""Persistence for learned infrared commands.

The analogue of `harmony_receiver.profiles.ButtonMap` for IR: a command is
identified only by the name it was given at learn time, and what is stored
is the raw mark/space timings a capture produced -- normalised, but never
decoded. There is no formula turning that back into "volume up" any more
than there is one for the RF signatures `ButtonMap` stores, and for the same
reason none is needed: a lookup table learned once per command is simpler
and more trustworthy than trusting a guessed bit layout to reproduce
hardware-critical timing.

One file per device (`ir_<device_id>.json`) rather than one shared file, so
removing a device's codes is a file delete and two devices can never corrupt
each other's codeset by racing to save at once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from ..storage import write_json

DEFAULT_DIR = "codes"


@dataclass
class IrCommand:
    """One learned command: a name, and the raw capture that reproduces it."""

    name: str
    label: str
    timings: List[int] = field(default_factory=list)
    repeats: int = 1
    repeatable: bool = False
    decoded: str = ""
    learned_at: str = ""


class CodeSet:
    """One device's learned commands, keyed by name."""

    def __init__(self, commands: Optional[Dict[str, IrCommand]] = None) -> None:
        self._commands: Dict[str, IrCommand] = commands or {}

    def __len__(self) -> int:
        return len(self._commands)

    def __iter__(self) -> Iterator[IrCommand]:
        return iter(sorted(self._commands.values(), key=lambda c: c.name))

    def __contains__(self, name: str) -> bool:
        return name in self._commands

    def get(self, name: str) -> Optional[IrCommand]:
        return self._commands.get(name)

    def add(
        self,
        name: str,
        label: str,
        timings: List[int],
        *,
        repeats: int = 1,
        repeatable: bool = False,
        decoded: str = "",
    ) -> IrCommand:
        """Records a command, replacing whatever it already held.

        Learning the same name again is a re-teach, not an error -- a remote
        that stopped responding to a stored code is fixed by pointing it at
        the receiver again, not by deleting the command first.
        """
        command = IrCommand(
            name=name,
            label=label,
            timings=list(timings),
            repeats=repeats,
            repeatable=repeatable,
            decoded=decoded,
            learned_at=datetime.now(timezone.utc).isoformat(),
        )
        self._commands[name] = command
        return command

    def forget(self, name: str) -> None:
        self._commands.pop(name, None)

    @classmethod
    def load(cls, path: "str | Path") -> "CodeSet":
        """Loads a codeset, or returns an empty one if the file doesn't exist yet.

        A missing file is a device that has never learned anything, not an
        error -- the learn screen is how the first command gets added.
        """
        path = Path(path)
        if not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return cls(
            {
                name: IrCommand(
                    name=name,
                    label=entry["label"],
                    timings=list(entry["timings"]),
                    repeats=entry.get("repeats", 1),
                    repeatable=entry.get("repeatable", False),
                    decoded=entry.get("decoded", ""),
                    learned_at=entry.get("learned_at", ""),
                )
                for name, entry in raw.items()
            }
        )

    def save(self, path: "str | Path") -> None:
        """Writes the codeset atomically, so a crash mid-save loses nothing.

        Shares `harmony_hub.storage.write_json` rather than open-coding the
        temp-file-and-rename dance a second time -- unlike `profiles.py`,
        this module already lives inside `harmony_hub` and has no reason to
        stay usable without it.
        """
        raw = {
            command.name: {
                "label": command.label,
                "timings": command.timings,
                "repeats": command.repeats,
                "repeatable": command.repeatable,
                "decoded": command.decoded,
                "learned_at": command.learned_at,
            }
            for command in self
        }
        write_json(raw, path)


def path_for(device_id: str, codes_dir: "str | Path" = DEFAULT_DIR) -> Path:
    """Where a device's codeset lives, given its configured (or default) directory."""
    return Path(codes_dir) / f"ir_{device_id}.json"
