"""Command-line entry point: brings up the web layer, then the hub under it.

The order matters and is the point of this file. The web server starts first
and unconditionally; the hub is started underneath it by `HubRuntime`, which
cannot take the server down with it. So a wrong address, an unplugged
FT232H or a corrupt configuration file leaves you with a settings page
explaining the problem rather than a process that exited.

Command-line flags are *overrides* for this run, layered over
`hub_settings.json`. They are not written back: the settings file belongs to
whoever edits it in the app, not to whichever shell command last started the
process.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import uvicorn

from harmony_receiver.protocol import HARMONY_CHANNELS

from . import settings as settings_module
from .api import create_app
from .settings import HubSettings

#: Set by `update.launcher` before exec'ing this process. Its presence is
#: what turns `/api/update` on at all (see `create_app`'s `update_root`) --
#: an ordinary `harmony-hub` run, launched directly, never has it set and
#: gets exactly today's behaviour.
UPDATE_ROOT_ENV = "HARMONY_UPDATE_ROOT"

logger = logging.getLogger("HUB")

#: Where to look for the built Flutter app, in order. The packaged copy wins
#: so an installed hub does not depend on the source tree still being there;
#: the build directory is the fallback that makes `flutter build web` in a
#: checkout Just Work without an extra copy step.
UI_DIRS = [
    Path(__file__).parent / "web",
    Path.cwd() / "app" / "build" / "web",
    Path(__file__).resolve().parents[2] / "app" / "build" / "web",
]


def find_ui_dir() -> Path:
    """The first built UI directory that exists, or the packaged path if none do."""
    return next((path for path in UI_DIRS if path.is_dir()), UI_DIRS[0])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harmony-hub",
        description="Scene engine and web UI for a Harmony remote. "
        "Every option here overrides hub_settings.json for this run only; "
        "edit and save settings in the app to make them stick.",
    )
    parser.add_argument("--settings", default=settings_module.DEFAULT_PATH, help="Hub settings JSON.")
    parser.add_argument("--host", default=None, help="Address to bind (default: all interfaces).")
    parser.add_argument("--port", type=int, default=None, help="Port to serve on (default: 8765).")
    parser.add_argument("--config", default=None, help="Hub configuration JSON.")
    parser.add_argument("--buttons", default=None, help="Button profile JSON from harmony-receiver.")
    parser.add_argument("--ui", default=None, help="Directory of built web UI files to serve.")
    parser.add_argument(
        "--no-autostart",
        action="store_true",
        help="Serve the UI but leave the hub stopped until it is started from Settings.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")

    source = parser.add_argument_group("event source")
    source.add_argument(
        "--source",
        choices=["none", "radio", "replay"],
        default=None,
        help="Where button events come from. 'none' still accepts simulated presses from the UI, "
        "which is enough to build and demonstrate scenes without a radio.",
    )
    source.add_argument("--replay", metavar="PATH", help="Capture JSONL to replay when --source replay.")
    source.add_argument("--replay-speed", type=float, default=None, help="Replay speed multiplier.")
    source.add_argument("--no-replay-loop", action="store_true", help="Play the capture once instead of looping.")

    radio = parser.add_argument_group("radio (when --source radio)")
    radio.add_argument("--address", metavar="HEX", help="Remote network address, e.g. 17129BFCB6.")
    radio.add_argument("--channel", type=int, choices=HARMONY_CHANNELS, help="Start on this channel.")
    radio.add_argument(
        "--probe-interval",
        type=float,
        default=None,
        help="Quiet seconds before re-locating the Hub by transmitting; 0 never transmits.",
    )
    radio.add_argument(
        "--allow-ack",
        action="store_true",
        help="Answer the remote. Correct when replacing the Hub, wrong while a real Hub is powered on.",
    )
    radio.add_argument("--csn-pin", default=None, help="FT232H pin wired to the nRF24's CSN.")
    radio.add_argument("--ce-pin", default=None, help="FT232H pin wired to the nRF24's CE.")
    return parser


def apply_overrides(settings: HubSettings, args: argparse.Namespace) -> HubSettings:
    """Layers command-line flags over saved settings.

    Only flags that were actually given win, which is why every default in
    the parser above is `None`: an unspecified `--port` must leave the saved
    port alone rather than quietly resetting it to 8765.
    """
    overrides = {
        "host": args.host,
        "port": args.port,
        "config_path": args.config,
        "buttons_path": args.buttons,
        "ui_dir": args.ui,
        "source": args.source,
        "replay_path": args.replay,
        "replay_speed": args.replay_speed,
        "address": args.address,
        "channel": args.channel,
        "probe_interval": args.probe_interval,
        "csn_pin": args.csn_pin,
        "ce_pin": args.ce_pin,
    }
    given = {key: value for key, value in overrides.items() if value is not None}

    # Store-true flags cannot be distinguished from "not given", so they only
    # ever turn something on -- never silently off.
    if args.no_replay_loop:
        given["replay_loop"] = False
    if args.allow_ack:
        given["allow_ack"] = True
    if args.no_autostart:
        given["autostart"] = False
    if args.verbose:
        given["verbose"] = True

    # Re-validated rather than `model_copy(update=...)`, which skips
    # validators outright: a `--address` that is not hex has to be rejected
    # here, not stored and then failed on much later by the radio.
    return HubSettings.model_validate({**settings.model_dump(), **given})


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    settings, settings_error = settings_module.load(args.settings)
    try:
        settings = apply_overrides(settings, args)
    except Exception as err:
        # A bad flag is worth failing on: nobody is watching a web page yet,
        # and silently ignoring it would be worse than saying so.
        print(f"harmony-hub: {err}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.DEBUG if settings.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    if settings_error:
        logger.warning("%s", settings_error)

    ui_dir = Path(settings.ui_dir) if settings.ui_dir else find_ui_dir()
    if not ui_dir.is_dir():
        logger.info(
            "No built web UI found (looked in %s); serving the API only. "
            "Run `flutter build web` in app/ to build it. API docs at /docs.",
            ", ".join(str(p) for p in UI_DIRS),
        )
    else:
        logger.info("Serving the web UI from %s", ui_dir)

    # Reported rather than fatal. The hub will refuse to start and say why,
    # on a page that is by then already serving -- which is where it can be
    # fixed without going back to a terminal.
    for problem in settings.problems():
        logger.warning("%s The hub will start once this is corrected in Settings.", problem)

    update_root = os.environ.get(UPDATE_ROOT_ENV)
    app = create_app(
        settings,
        static_dir=ui_dir,
        settings_path=args.settings,
        settings_error=settings_error,
        update_root=update_root,
    )
    logger.info("Hub on http://%s:%d  (API docs at /docs)", settings.host, settings.port)

    # Built explicitly rather than via `uvicorn.run(...)` -- which is a thin
    # wrapper around exactly this -- so `/api/update` can reach the `Server`
    # instance through `app.state.uvicorn_server` and ask it to stop, the
    # same way it would stop for SIGTERM, without this process needing to
    # send a signal to itself.
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="debug" if settings.verbose else "warning",
        # Belt and braces: a task that will not finish (a stuck backend
        # close, a wedged radio thread) should cost a few seconds on exit,
        # not a Ctrl+C that never returns.
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    server.run()

    if update_root and getattr(app.state, "restart_requested", False):
        _reexec_into_launcher(update_root)
    return 0


def _reexec_into_launcher(update_root: str) -> None:
    """Re-enters `update.launcher` in this same OS process, onto whatever release it now activates.

    Not a fresh `harmony-hub` invocation: the launcher itself has to run
    again first, since it owns the trial-attempt bookkeeping and the
    decision to roll back a release that keeps failing. Works the same way
    whether this process is supervised by systemd or started by hand --
    `execve` replaces this process's image in place, so nothing about how it
    was started has to notice a restart happened at all.
    """
    launcher = Path(update_root) / "bin" / "harmony-launch"
    logger.info("Restarting via %s", launcher)
    os.execve(sys.executable, [sys.executable, str(launcher), str(update_root)], os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main())
