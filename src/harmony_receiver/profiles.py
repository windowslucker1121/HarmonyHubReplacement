"""Persistence for reverse-engineered button identities.

A press is identified by its *signature*: the hex of bytes 1..4 of the first
packet the remote sends (see `protocol`). There is no known formula turning
those four bytes into a button, and none is needed -- the set of buttons on a
remote is small and fixed, so a lookup table learned once per button is both
simpler and more trustworthy than a guessed bit layout.

A button can legitimately own more than one signature (the same key can be
reported differently depending on the active activity), which is why each
profile holds a set.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, Optional

DEFAULT_PATH = "buttons.json"


@dataclass
class ButtonProfile:
    """A named button and every distinct signature seen while it was pressed."""

    key: str
    label: str
    signatures: set = field(default_factory=set)


class ButtonMap:
    """A collection of button profiles with a reverse signature -> button index.

    Lookups happen on every single press, so the reverse index is maintained
    alongside the profiles rather than rebuilt by scanning them each time.
    """

    def __init__(self, profiles: Optional[Dict[str, ButtonProfile]] = None) -> None:
        self._profiles: Dict[str, ButtonProfile] = profiles or {}
        self._by_signature: Dict[str, ButtonProfile] = {}
        for profile in self._profiles.values():
            for signature in profile.signatures:
                self._by_signature[signature] = profile

    def __len__(self) -> int:
        return len(self._profiles)

    def __iter__(self) -> Iterator[ButtonProfile]:
        return iter(sorted(self._profiles.values(), key=lambda p: p.key))

    def __contains__(self, key: str) -> bool:
        return key in self._profiles

    def identify(self, signature: str) -> Optional[ButtonProfile]:
        """Finds the button that produced this signature, if it has been learned."""
        return self._by_signature.get(signature.upper())

    def resolve(self, signature: str) -> Optional[str]:
        """The friendly label for a signature, or None -- the shape `PressTracker` wants."""
        profile = self.identify(signature)
        return profile.label if profile else None

    def learn(self, key: str, label: str, signature: str) -> ButtonProfile:
        """Records a signature as belonging to the named button, creating it if new."""
        signature = signature.upper()
        profile = self._profiles.get(key)
        if profile is None:
            profile = ButtonProfile(key=key, label=label)
            self._profiles[key] = profile
        profile.label = label
        profile.signatures.add(signature)
        self._by_signature[signature] = profile
        return profile

    def forget(self, key: str) -> None:
        """Drops a button and all of its signatures."""
        profile = self._profiles.pop(key, None)
        if profile is None:
            return
        for signature in profile.signatures:
            self._by_signature.pop(signature, None)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> "ButtonMap":
        """Loads button profiles from a JSON file, or returns an empty map if absent."""
        path = Path(path)
        if not path.exists():
            return cls()

        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        return cls(
            {
                key: ButtonProfile(key=key, label=entry["label"], signatures=set(entry["signatures"]))
                for key, entry in raw.items()
            }
        )

    def save(self, path: str | Path = DEFAULT_PATH) -> None:
        """Writes button profiles to JSON, sorted for stable, diffable output.

        Written to a temporary file and renamed over the target, so an
        interrupted write leaves the previous map intact. This file is the
        product of someone pressing every button on a remote one at a time --
        expensive to rebuild, and now written from a web UI where two saves
        can overlap, so a truncated file is worth ruling out.

        Deliberately open-coded rather than shared with `harmony_hub.storage`:
        this package stays usable without the hub installed.
        """
        raw = {
            profile.key: {"label": profile.label, "signatures": sorted(profile.signatures)}
            for profile in self
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
