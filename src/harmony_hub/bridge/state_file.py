"""Remembers what was last published to the discovery topic, across restarts.

Home Assistant's device-discovery removal dance (see `discovery.diff_removed`)
needs to know which component keys a *previous* publish already told Home
Assistant about, so a scene deleted five minutes ago -- or five restarts ago
-- still gets cleaned up rather than lingering as a dead entity. Reading that
back from the broker itself would mean subscribing to the discovery topic
and racing its retained message against a timeout; a small JSON file next to
`hub_settings.json` answers the same question without touching the network,
in the same spirit as `update/state.json` and `update/check.py`'s cache.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

from ..storage import write_json

logger = logging.getLogger("HUB.bridge.state_file")

DEFAULT_PATH = "mqtt_bridge_state.json"


def load_components(path: str | Path = DEFAULT_PATH) -> Dict[str, str]:
    """The component-key -> platform map from the last successful publish.

    A missing or corrupt file reads as "nothing published yet" rather than
    an error -- the same "never raise" rule every other loader here follows,
    since the worst this can do wrong is one redundant removal dance the
    next time discovery is published.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        components = data.get("components", {})
        return {str(k): str(v) for k, v in components.items()}
    except Exception as err:
        logger.warning("Could not read %s: %s -- starting from an empty component map", path, err)
        return {}


def save_components(components: Dict[str, str], path: str | Path = DEFAULT_PATH) -> None:
    write_json({"components": components}, path)
