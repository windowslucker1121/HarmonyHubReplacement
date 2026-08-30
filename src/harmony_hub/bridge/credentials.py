"""Where the MQTT broker password lives -- not in `hub_settings.json`.

Same reasoning as `backends/homeassistant.py`'s access token: a broker
password is a secret, `GET /api/settings` is readable by anything on the
LAN, and the settings screen already has a text field for the broker
address right next to where this one would otherwise sit. Splitting it out
means `hub_settings.json` can be copied, committed to a private dotfiles
repo, or pasted into a bug report without leaking it.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CREDENTIALS_DIR = "credentials"


def password_path(node_id: str, directory: str | Path = DEFAULT_CREDENTIALS_DIR) -> Path:
    return Path(directory) / f"mqtt_{node_id}.password"


def read_password(node_id: str, directory: str | Path = DEFAULT_CREDENTIALS_DIR) -> str:
    try:
        return password_path(node_id, directory).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_password(node_id: str, password: str, directory: str | Path = DEFAULT_CREDENTIALS_DIR) -> None:
    path = password_path(node_id, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(password.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows and most network shares do not implement this -- the file
        # is inside a gitignored directory either way, so this is a bonus
        # rather than something worth failing the save over.
        pass


def clear_password(node_id: str, directory: str | Path = DEFAULT_CREDENTIALS_DIR) -> None:
    password_path(node_id, directory).unlink(missing_ok=True)
