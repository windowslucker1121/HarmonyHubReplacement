"""Pairs a configured device from a terminal.

The app is the normal way to do this -- add the device, tap Pair, type the
code. This exists for the case the app cannot cover: a hub running headless
on a box with no built UI, or a first device added by hand to the JSON.

It works for any backend implementing `backends.Pairable`, not just Android
TV, and pairs the device exactly as configured, so whatever the pairing leaves
behind lands where the running hub will look for it.

    python -m harmony_hub.pair shield

Run it while the hub is stopped: two clients pairing the same device at once
would race for the same certificate file.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import backends
from . import config as config_module

logger = logging.getLogger("HUB.pair")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harmony_hub.pair",
        description="Pairs a configured device that needs a code confirmed on screen.",
    )
    parser.add_argument("device", nargs="?", help="Device id from the hub configuration.")
    parser.add_argument("--config", default=config_module.DEFAULT_PATH, help="Hub configuration JSON.")
    parser.add_argument("--code", help="Pairing code, if you would rather not be prompted.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return parser


def _pairable_ids(config_path: str) -> list[str]:
    """Configured devices that need pairing, for the 'which one?' message."""
    ids = []
    for device in config_module.load(config_path).devices:
        try:
            if issubclass(backends.get(device.backend), backends.Pairable):
                ids.append(device.id)
        except KeyError:
            # A device naming a backend this install does not have is the
            # hub's problem to report, not this helper's.
            continue
    return ids


async def pair(device_id: str, config_path: str, code: str | None) -> int:
    config = config_module.load(config_path)
    device = config.device(device_id)
    if device is None:
        known = ", ".join(d.id for d in config.devices) or "none configured"
        print(f"No device '{device_id}' in {config_path} (have: {known})", file=sys.stderr)
        return 2

    backend = backends.create(device.backend, device.id, device.config)
    if not isinstance(backend, backends.Pairable):
        print(f"Device '{device_id}' uses the {device.backend} backend, which does not pair.")
        return 2

    await backend.connect()
    try:
        print(await backend.pair_start())
        if code is None:
            if backend.pair_input_label:
                # The backend names what it is asking for: a television
                # wants a code, a Home Assistant wants a token, and
                # prompting for the wrong one is a small lie that costs
                # someone a minute.
                code = input(f"{backend.pair_input_label}: ")
            else:
                # Nothing to type back -- the hint already said what to do
                # on the device itself. Just wait for Enter.
                input("Press Enter once that's done: ")
                code = ""
        await backend.pair_finish(code)
    except backends.BackendError as err:
        print(f"Pairing failed: {err}", file=sys.stderr)
        return 1
    finally:
        await backend.close()

    print(f"Paired '{device_id}'. Start the hub and it will connect on its own.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.device:
        print("Which device? " + (", ".join(_pairable_ids(args.config)) or "nothing configured needs pairing."))
        return 2

    return asyncio.run(pair(args.device, args.config, args.code))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
