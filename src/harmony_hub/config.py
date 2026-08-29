"""Loading and saving the hub configuration."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import HubConfig
from .storage import write_json

logger = logging.getLogger("HUB.config")

DEFAULT_PATH = "hub_config.json"


def load(path: str | Path = DEFAULT_PATH) -> HubConfig:
    """Reads the configuration, or returns an empty one if the file doesn't exist yet.

    A missing file is a first run, not an error -- the UI is how the first
    device gets added, and it needs something to start from.
    """
    path = Path(path)
    if not path.exists():
        logger.info("No configuration at %s; starting empty", path)
        return HubConfig()

    with path.open("r", encoding="utf-8") as f:
        return HubConfig.model_validate(json.load(f))


def save(config: HubConfig, path: str | Path = DEFAULT_PATH) -> None:
    """Writes the configuration atomically, so a crash mid-write loses nothing."""
    write_json(config.model_dump(mode="json", exclude_defaults=False), path)
    logger.info("Saved configuration to %s", path)
