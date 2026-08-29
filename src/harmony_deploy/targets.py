"""deploy_targets.json: which devices harmony-deploy knows about, and how to reach them.

Per-machine and gitignored -- see deploy_targets.example.json for the
shape. `harmony-deploy setup` is the only thing that writes to it; every
other command only reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from harmony_hub.update import auth as update_auth

from .errors import DeployError

DEFAULT_TARGETS_FILE = Path(__file__).resolve().parents[2] / "deploy_targets.json"


def load_targets(path: Path = DEFAULT_TARGETS_FILE) -> Dict[str, Any]:
    """Reads deploy_targets.json. A missing file is an error -- for commands that only read."""
    if not path.exists():
        raise DeployError(
            f"{path} does not exist yet. Create it, for example:\n"
            "  { \"pi\": { \"url\": \"http://harmony.local:8765\", \"token_file\": \"~/.harmony/pi.token\" } }\n"
            "See deploy_targets.example.json, or run `harmony-deploy setup` to create the first entry."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def load_targets_or_empty(path: Path = DEFAULT_TARGETS_FILE) -> Dict[str, Any]:
    """Like `load_targets`, but a missing file just means "nothing recorded yet" -- for `setup`, which writes the first one."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_target(name: str, targets: Dict[str, Any]) -> Dict[str, Any]:
    if name not in targets:
        known = ", ".join(sorted(targets)) or "(none configured)"
        raise DeployError(f"unknown target: {name}. Known targets: {known}")
    return targets[name]


def save_target(name: str, entry: Dict[str, Any], path: Path = DEFAULT_TARGETS_FILE) -> None:
    """Adds or replaces one target's entry, leaving every other target already in the file untouched."""
    targets = load_targets_or_empty(path)
    targets[name] = entry
    path.write_text(json.dumps(targets, indent=2) + "\n", encoding="utf-8")


def read_token(token_file: str) -> bytes:
    path = Path(token_file).expanduser()
    if not path.exists():
        raise DeployError(
            f"token file {path} does not exist -- copy the device's data/update_token there over SSH first, "
            "or run `harmony-deploy setup` to do that automatically"
        )
    token = path.read_bytes()
    if len(token) != update_auth.TOKEN_BYTES:
        raise DeployError(f"{path} does not look like a {update_auth.TOKEN_BYTES}-byte update token")
    return token
