"""How this hub is deployed: where it listens, and where its events come from.

Kept in a separate file from `hub_config.json` on purpose. That file holds
devices and scenes -- the user's own data, rewritten by the app on every
edit. These are deployment settings with a different lifecycle, and a bad
scene edit must not be able to strand the radio configuration.

The two tiers of validation here are the load-bearing part. Field-level rules
are strict, so a nonsense port is rejected outright. Cross-field rules are
*advisory* (`problems()`), because "source is radio but the address is blank"
has to be a saveable state -- that is precisely the state you are in while
you go and find the address.
"""

from __future__ import annotations

import json
import logging
import platform
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import Field, field_validator

from harmony_receiver.profiles import DEFAULT_PATH as BUTTONS_PATH
from harmony_receiver.protocol import HARMONY_CHANNELS

from . import config as config_module
from .models import Base
from .storage import write_json

logger = logging.getLogger("HUB.settings")

DEFAULT_PATH = "hub_settings.json"

Source = Literal["none", "radio", "replay"]


class HubSettings(Base):
    """Everything needed to start a hub, in one place.

    Split into what the web server needs at process start (`host`, `port`,
    `ui_dir`) and what the hub itself needs (everything else). The first
    group is editable and saved but only read once, at boot: rebinding a live
    listener would move the page's URL out from under whoever is editing it.
    """

    # -- Web server. Read once at process start; see `needs_process_restart`.
    host: str = "0.0.0.0"
    port: int = Field(default=8765, ge=1, le=65535)
    ui_dir: Optional[Path] = None

    # -- Files
    config_path: Path = Path(config_module.DEFAULT_PATH)
    buttons_path: Path = Path(BUTTONS_PATH)

    # -- Event source
    #: Whether to bring the hub up as soon as the process starts. Off is for
    #: an install that boots into a known-broken state and needs configuring
    #: before it touches any equipment.
    autostart: bool = True

    #: "radio" reads the real remote, "replay" a recorded capture, "none"
    #: leaves only the API's simulated presses -- which is enough to build
    #: and demonstrate the whole platform with nobody holding a remote.
    source: Source = "none"

    replay_path: Optional[Path] = None
    replay_speed: float = Field(default=1.0, gt=0, le=100)
    replay_loop: bool = True

    # -- Radio
    address: Optional[str] = None
    channel: Optional[int] = None
    probe_interval: float = Field(default=0.0, ge=0, le=3600)
    allow_ack: bool = False
    csn_pin: str = "C0"
    ce_pin: str = "D4"

    # -- Infrared. One receiver and one transmitter per install, wired once
    # and shared by every IR device configured against it -- the same way
    # every radio-backed device shares `csn_pin`/`ce_pin` above, rather than
    # each carrying its own copy of a fact about the install, not the device.
    #: BCM GPIO numbers, addressed directly through pigpio rather than
    #: through Blinka's board names -- pigpio has no notion of an FT232H
    #: breakout, so these only mean anything on a Pi. `None` means "not
    #: wired": a receive-only or transmit-only install leaves the other
    #: blank rather than the pair being all-or-nothing.
    ir_rx_pin: Optional[int] = Field(default=None, ge=0, le=27)
    ir_tx_pin: Optional[int] = Field(default=None, ge=0, le=27)
    ir_pigpio_host: str = "localhost"
    ir_pigpio_port: int = Field(default=8888, ge=1, le=65535)

    verbose: bool = False

    #: Whether `/api/update` accepts a push at all. Off refuses every
    #: request before the signature is even checked, for an install that
    #: would rather require a person at the device (or over SSH) than trust
    #: the network at all.
    updates_enabled: bool = True

    @field_validator("address")
    @classmethod
    def _check_address(cls, value: Optional[str]) -> Optional[str]:
        """Five bytes of hex, stored uppercase.

        Empty means "not set yet" rather than an error: the settings form
        submits a cleared field as an empty string, and clearing an address
        you are about to replace has to be allowed.
        """
        if value is None or not value.strip():
            return None
        text = value.strip().upper()
        if len(text) != 10 or any(c not in "0123456789ABCDEF" for c in text):
            raise ValueError("address must be 10 hex digits, e.g. 17129BFCB6")
        return text

    @field_validator("channel")
    @classmethod
    def _check_channel(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value not in HARMONY_CHANNELS:
            raise ValueError(f"channel must be one of {HARMONY_CHANNELS}")
        return value

    # ------------------------------------------------------------------

    def problems(self) -> List[str]:
        """Why the hub would refuse to start with these settings, in plain words.

        Advisory rather than fatal, so these can be saved and fixed at
        leisure. The supervisor checks them before starting; the settings
        page shows them inline as you type.
        """
        problems = []
        if self.source == "radio" and not self.address:
            problems.append("Source is 'radio' but no remote address is set.")
        if self.source == "replay":
            if not self.replay_path:
                problems.append("Source is 'replay' but no capture file is set.")
            elif not Path(self.replay_path).is_file():
                problems.append(f"Capture file '{self.replay_path}' does not exist.")
        return problems

    def radio_gpio(self) -> "set[int]":
        """The BCM GPIO numbers the radio holds, for spotting an IR pin collision.

        Only meaningful on a Raspberry Pi. There, Blinka's generic Linux board
        module maps `csn_pin`/`ce_pin` names like `"D5"` straight onto BCM
        GPIO5 -- see the wiring table in RASPBERRY_PI_DEPLOYMENT.md, which is
        already written in exactly those terms. On the FT232H breakout used
        for dev work off Linux, the same-looking names (`"C0"`, `"D4"`)
        address pins on the USB bridge itself, not the host's own GPIO header,
        so there is nothing there for an IR pin to collide with and this
        returns nothing.
        """
        if platform.system() != "Linux":
            return set()
        pins = set()
        for name in (self.csn_pin, self.ce_pin):
            text = name.strip().upper()
            if text.startswith("D") and text[1:].isdigit():
                pins.add(int(text[1:]))
        return pins

    def needs_process_restart(self, live: "HubSettings") -> bool:
        """Whether these differ from `live` in a way only a reboot can apply."""
        return (
            self.host != live.host
            or self.port != live.port
            or self.ui_dir != live.ui_dir
        )

    def describe_source(self) -> str:
        """One line for the UI: what this hub is listening to."""
        if self.source == "radio":
            return f"Radio {self.address or '(no address)'}"
        if self.source == "replay":
            return f"Replay of {Path(self.replay_path).name if self.replay_path else '(no file)'}"
        return "Simulated presses only"


def load(path: str | Path = DEFAULT_PATH) -> "tuple[HubSettings, Optional[str]]":
    """Reads settings, falling back to defaults rather than raising.

    Returns the settings and, when something was wrong with the file, the
    reason. Never raising is the whole point: a settings file with a typo in
    it must not stop the web server, because the web server is where the typo
    gets fixed.
    """
    path = Path(path)
    if not path.exists():
        logger.info("No settings at %s; using defaults", path)
        return HubSettings(), None

    try:
        with path.open("r", encoding="utf-8") as f:
            return HubSettings.model_validate(json.load(f)), None
    except Exception as err:
        logger.error("Could not read settings from %s: %s", path, err)
        return HubSettings(), f"{path} could not be read ({err}); using defaults"


def save(settings: HubSettings, path: str | Path = DEFAULT_PATH) -> None:
    write_json(settings.model_dump(mode="json"), path)
    logger.info("Saved settings to %s", path)
