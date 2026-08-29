"""Answers "is this hub actually going to work?" without having to find out the hard way.

Two entry points, deliberately returning the same shape so the app renders
one component for both:

* `run_checks` inspects the hub as it is configured right now.
* `try_settings` does the same against settings that have not been saved,
  which is how a radio address or a capture file gets verified before it is
  committed.

Both go through `sources.build_source`, the same function the real hub
starts through. A separate implementation would eventually answer a subtly
different question than the one being asked.
"""

from __future__ import annotations

import json
import logging
import os
import platform
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from pydantic import BaseModel

from . import config as config_module
from .ir import codes as ir_codes
from .ir import gateway as ir_gateway
from .settings import HubSettings
from .sources import build_source

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import HubRuntime

logger = logging.getLogger("HUB.diagnostics")


class Check(BaseModel):
    """One thing that was verified, and what came of it."""

    name: str
    ok: bool
    detail: str = ""


def _writable(path: Path) -> "tuple[bool, str]":
    """Whether this path could be written, without writing anything to it."""
    if path.exists():
        if os.access(path, os.W_OK):
            return True, f"{path} is writable"
        return False, f"{path} exists but is read-only"
    parent = path.parent if str(path.parent) else Path(".")
    if parent.is_dir() and os.access(parent, os.W_OK):
        return True, f"{path} does not exist yet; {parent} is writable"
    return False, f"cannot write to {parent}"


def _check_settings_file(runtime: "HubRuntime") -> Check:
    if runtime.settings_error:
        return Check(name="Settings file", ok=False, detail=runtime.settings_error)
    ok, detail = _writable(Path(runtime.settings_path))
    return Check(name="Settings file", ok=ok, detail=detail)


def _check_config_file(runtime: "HubRuntime") -> Check:
    if runtime.config_error:
        return Check(name="Configuration file", ok=False, detail=runtime.config_error)

    path = Path(runtime.settings.config_path)
    ok, detail = _writable(path)
    if not ok:
        return Check(name="Configuration file", ok=False, detail=f"{detail} -- edits could not be saved")

    counts = f"{len(runtime.config.devices)} device(s), {len(runtime.config.scenes)} scene(s)"
    if not path.exists():
        return Check(name="Configuration file", ok=True, detail=f"not created yet; {counts} in memory")
    return Check(name="Configuration file", ok=True, detail=f"{path}: {counts}")


def _check_buttons(runtime: "HubRuntime") -> Check:
    path = Path(runtime.settings.buttons_path)
    count = len(runtime.buttons)
    if count:
        return Check(name="Button map", ok=True, detail=f"{count} button(s) from {path}")
    if not path.exists():
        return Check(
            name="Button map",
            ok=False,
            detail=f"no button map at {path} -- run `harmony-receiver learn` to build one",
        )
    return Check(name="Button map", ok=False, detail=f"{path} holds no buttons")


def _replay_check(settings: HubSettings) -> Check:
    path = Path(settings.replay_path) if settings.replay_path else None
    if path is None:
        return Check(name="Event source", ok=False, detail="source is 'replay' but no capture file is set")
    if not path.is_file():
        return Check(name="Event source", ok=False, detail=f"no capture file at {path}")
    try:
        packets = sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("type") == "packet"
        )
    except Exception as err:
        return Check(name="Event source", ok=False, detail=f"{path} could not be read: {err}")
    if not packets:
        return Check(name="Event source", ok=False, detail=f"{path} contains no packets to replay")
    return Check(name="Event source", ok=True, detail=f"{path}: {packets} packet(s) to replay")


def _probe_radio(settings: HubSettings, name: str = "Event source") -> Check:
    """Opens the radio for real, then hands the pins straight back.

    Releasing matters as much as opening: `DigitalInOut` claims a pin for the
    life of the process, so a check that kept them would make the next hub
    start fail with an error indistinguishable from the radio being unplugged.
    """
    if not settings.address:
        return Check(name=name, ok=False, detail="source is 'radio' but no remote address is set")

    try:
        from harmony_receiver.radio import create_radio, release_radio
    except Exception as err:  # pragma: no cover - import-time hardware stack
        return Check(name=name, ok=False, detail=f"radio support is not installed: {err}")

    try:
        radio = create_radio(settings.csn_pin, settings.ce_pin)
    except Exception as err:
        return Check(
            name=name,
            ok=False,
            detail=f"could not open the radio on CSN={settings.csn_pin}/CE={settings.ce_pin}: {err}",
        )

    try:
        channel = f"channel {settings.channel}" if settings.channel else "no start channel"
        return Check(name=name, ok=True, detail=f"radio ready for {settings.address} ({channel})")
    finally:
        release_radio(radio)


def _check_source(runtime: "HubRuntime") -> Check:
    settings = runtime.settings
    if settings.source == "none":
        return Check(
            name="Event source",
            ok=True,
            detail="simulated presses only -- no remote is being listened to",
        )
    if settings.source == "replay":
        return _replay_check(settings)

    if runtime.service is not None and runtime.service.uses_radio:
        # Probing now would fight the running hub for the same hardware, and
        # the hub already holding it is the answer the check is looking for.
        return Check(name="Event source", ok=True, detail=f"radio in use by the running hub ({settings.address})")
    return _probe_radio(settings)


#: Reserved for the Pi's own hardware SPI0 bus (SCK/MOSI/MISO) -- see the
#: wiring table in RASPBERRY_PI_DEPLOYMENT.md. Only meaningful on Linux, the
#: same restriction `HubSettings.radio_gpio` documents for the same reason:
#: off Linux these are FT232H breakout pin numbers, not GPIO.
_PI_SPI0_GPIO = {9, 10, 11}


def _check_ir(runtime: "HubRuntime") -> Optional[Check]:
    """Whether the one wired IR receiver/transmitter is sanely set up.

    Distinct from what `_check_devices` already reports: a device's own
    `health()` says whether *it* can reach the gateway, but that only runs
    once a device exists to ask. This is the install-wide wiring sanity that
    makes sense checked exactly once -- the RX/TX pins colliding with each
    other, with the radio, or with the Pi's own SPI0 bus -- and it is
    available even before a single IR device has been configured.

    Omitted entirely (returns `None`) when there is nothing to say: no pins
    wired and no IR device configured, the same way `_check_devices` reports
    nothing for a configuration with no devices in it at all.
    """
    settings = runtime.settings
    ir_devices = [d for d in runtime.config.devices if d.backend == "ir"]
    if settings.ir_rx_pin is None and settings.ir_tx_pin is None and not ir_devices:
        return None

    blocking: List[str] = []
    if settings.ir_rx_pin is not None and settings.ir_rx_pin == settings.ir_tx_pin:
        blocking.append(f"receive and transmit are both GPIO{settings.ir_rx_pin}")

    pins = {p for p in (settings.ir_rx_pin, settings.ir_tx_pin) if p is not None}
    radio_collision = pins & settings.radio_gpio()
    if radio_collision:
        blocking.append(f"GPIO{sorted(radio_collision)[0]} is also the radio's CSN/CE pin")

    if platform.system() == "Linux":
        spi_collision = pins & _PI_SPI0_GPIO
        if spi_collision:
            blocking.append(f"GPIO{sorted(spi_collision)[0]} is reserved for the Pi's own SPI0 bus")

    if blocking:
        return Check(name="Infrared", ok=False, detail="; ".join(blocking))

    notes: List[str] = []
    if settings.ir_rx_pin is None:
        notes.append("no receive pin -- sending works, learning does not")
    if settings.ir_tx_pin is None:
        notes.append("no transmit pin -- learning works, sending does not")
    if ir_devices:
        code_count = sum(
            len(
                ir_codes.CodeSet.load(
                    ir_codes.path_for(d.id, d.config.get("codes_dir") or ir_codes.DEFAULT_DIR)
                )
            )
            for d in ir_devices
        )
        notes.append(f"{code_count} code(s) across {len(ir_devices)} device(s)")

    wiring = [
        f"receive GPIO{settings.ir_rx_pin}" if settings.ir_rx_pin is not None else None,
        f"transmit GPIO{settings.ir_tx_pin}" if settings.ir_tx_pin is not None else None,
    ]
    detail = ", ".join([w for w in wiring if w] + notes)

    # Everything above is settings/file-based and answers "is this sanely
    # configured" even with the hub stopped. Whether it actually *works* is
    # a different question this check used to leave entirely to whichever
    # IR device's own row `_check_devices` prints further down -- which
    # only exists once a device has been configured, and even then reads as
    # a second, easy-to-miss row rather than the direct answer this one
    # looks like it's already giving. Probed only while the hub is running:
    # a stopped hub has deliberately released the gateway (see
    # `HubService.stop`), so `health()` would just report "not configured"
    # regardless of whether the wiring above is actually fine.
    if runtime.service is not None and (settings.ir_rx_pin is not None or settings.ir_tx_pin is not None):
        gateway_ok, gateway_detail = ir_gateway.gateway().health()
        if not gateway_ok:
            remedy = " (sudo systemctl enable --now pigpiod)" if "not reachable" in gateway_detail else ""
            return Check(name="Infrared", ok=False, detail=f"{detail} -- {gateway_detail}{remedy}")

    return Check(name="Infrared", ok=True, detail=detail)


def _check_ui(static_dir: Optional[Path]) -> Check:
    if static_dir is None or not Path(static_dir).is_dir():
        return Check(
            name="Web interface",
            ok=False,
            detail="no built web UI found -- run `flutter build web` in app/. The API still works.",
        )
    return Check(name="Web interface", ok=True, detail=f"serving from {static_dir}")


async def _check_devices(runtime: "HubRuntime") -> List[Check]:
    if runtime.service is None:
        if not runtime.config.devices:
            return []
        return [
            Check(
                name=f"Device: {device.name}",
                ok=False,
                detail="not started -- the hub is stopped",
            )
            for device in runtime.config.devices
        ]

    checks = []
    for device in runtime.config.devices:
        backend = runtime.service.engine.backend_for(device.id)
        if backend is None:
            checks.append(Check(name=f"Device: {device.name}", ok=False, detail="failed to start"))
            continue
        try:
            health = await backend.health()
            checks.append(Check(name=f"Device: {device.name}", ok=health.ok, detail=health.detail))
        except Exception as err:
            checks.append(Check(name=f"Device: {device.name}", ok=False, detail=str(err)))
    return checks


async def run_checks(runtime: "HubRuntime", static_dir: Optional[Path] = None) -> List[Check]:
    """Everything worth verifying about this hub, in the order it matters."""
    checks = [
        _check_settings_file(runtime),
        _check_config_file(runtime),
        _check_buttons(runtime),
        _check_source(runtime),
        _check_ui(static_dir),
    ]
    ir_check = _check_ir(runtime)
    if ir_check is not None:
        checks.append(ir_check)
    checks.extend(await _check_devices(runtime))
    return checks


async def try_settings(runtime: "HubRuntime", settings: HubSettings) -> List[Check]:
    """Whether settings that have not been saved would actually work.

    Builds the event source they describe and immediately closes it, so the
    answer comes from the same code path the hub starts through rather than
    from a guess about it.
    """
    checks: List[Check] = []

    for problem in settings.problems():
        checks.append(Check(name="Settings", ok=False, detail=problem))
    if not checks:
        checks.append(Check(name="Settings", ok=True, detail="no missing or contradictory values"))

    config_path = Path(settings.config_path)
    if config_path.exists():
        try:
            config = config_module.load(config_path)
            checks.append(
                Check(
                    name="Configuration file",
                    ok=True,
                    detail=f"{config_path}: {len(config.devices)} device(s), {len(config.scenes)} scene(s)",
                )
            )
        except Exception as err:
            checks.append(Check(name="Configuration file", ok=False, detail=f"{config_path}: {err}"))
    else:
        ok, detail = _writable(config_path)
        checks.append(Check(name="Configuration file", ok=ok, detail=detail))

    buttons_path = Path(settings.buttons_path)
    checks.append(
        Check(
            name="Button map",
            ok=buttons_path.is_file(),
            detail=f"{buttons_path}" + ("" if buttons_path.is_file() else " does not exist"),
        )
    )

    checks.append(await _try_source(runtime, settings))
    return checks


async def _try_source(runtime: "HubRuntime", settings: HubSettings) -> Check:
    if settings.source == "none":
        return Check(name="Event source", ok=True, detail="simulated presses only -- nothing to open")
    if settings.source == "replay":
        return _replay_check(settings)

    if runtime.service is not None and runtime.service.uses_radio:
        return Check(
            name="Event source",
            ok=False,
            detail="the running hub is holding the radio -- stop it to test these settings",
        )

    # `build_source` opens the hardware, so the radio has to be handed back
    # here too; `RadioSource.close()` alone would leave the pins claimed.
    try:
        source = build_source(settings)
    except Exception as err:
        return Check(name="Event source", ok=False, detail=str(err))

    try:
        return Check(name="Event source", ok=True, detail=f"{settings.describe_source()} opened successfully")
    finally:
        if source is not None:
            try:
                await source.close()
            except Exception:
                logger.debug("Dry-run source would not close", exc_info=True)
