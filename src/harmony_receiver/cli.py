"""Command-line entry point for pairing, capturing, and decoding Harmony traffic.

The reverse-engineering commands (`capture`, `analyze`, `learn`) are split so
that only `capture` needs the radio. Sessions get recorded to JSONL once and
can then be re-analysed as often as the protocol understanding improves,
without asking anyone to press the same button again.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

from .capture import CaptureLog
from .pairing import PairingTimeout, discover_network_address, sniff_network_address
from .profiles import DEFAULT_PATH, ButtonMap
from .protocol import HARMONY_CHANNELS, parse_frame
from .radio import DEFAULT_CE_PIN, DEFAULT_CSN_PIN, create_radio, set_silent
from .receiver import DEFAULT_PROBE_INTERVAL, DEFAULT_DWELL, HarmonyReceiver

logger = logging.getLogger("CLI")


def _add_radio_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--csn-pin",
        default=DEFAULT_CSN_PIN,
        help=f"Pin wired to the nRF24's CSN -- an FT232H pin (default: {DEFAULT_CSN_PIN}) on Windows/macOS, "
        "or a Raspberry Pi GPIO name like D5 when running natively on Linux.",
    )
    parser.add_argument(
        "--ce-pin",
        default=DEFAULT_CE_PIN,
        help=f"Pin wired to the nRF24's CE -- an FT232H pin (default: {DEFAULT_CE_PIN}) on Windows/macOS, "
        "or a Raspberry Pi GPIO name like D6 when running natively on Linux.",
    )


def _add_listen_args(parser: argparse.ArgumentParser) -> None:
    _add_radio_args(parser)
    parser.add_argument(
        "--address", metavar="HEX", required=True, help="The 5-byte network address, e.g. 17129BFCB6."
    )
    parser.add_argument(
        "--channel",
        type=int,
        choices=HARMONY_CHANNELS,
        help="Skip the channel search and listen here immediately.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Transmit one ping sweep at startup to locate the Hub's channel, then go silent. "
        "Faster to lock, but it briefly puts packets on the air.",
    )
    parser.add_argument(
        "--allow-ack",
        action="store_true",
        help="Leave auto-ACK enabled while capturing. This makes the radio answer the remote and "
        "collide with the real Hub's ACK -- only for A/B testing the receive path.",
    )
    parser.add_argument(
        "--probe-interval",
        type=float,
        default=DEFAULT_PROBE_INTERVAL,
        metavar="SECONDS",
        help="Quiet time before re-locating the Hub by transmitting a ping sweep. "
        "0 disables it entirely: never transmit, stay on one channel, and wait for the "
        f"remote's own channel search to reach us (default: {DEFAULT_PROBE_INTERVAL}).",
    )
    parser.add_argument(
        "--dwell",
        type=float,
        default=DEFAULT_DWELL,
        metavar="SECONDS",
        help=f"Time spent on each channel while searching (default: {DEFAULT_DWELL}).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harmony-receiver",
        description="Capture and decode Logitech Harmony remote traffic via an nRF24L01+ radio.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Log every raw packet as it arrives.")
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("pair", help="Handshake with a Hub in pairing mode to learn the network address.")
    _add_radio_args(pair)
    pair.add_argument("--timeout", type=float, default=60, metavar="SECONDS", help="Give up after this long.")
    pair.add_argument(
        "--without-hub",
        action="store_true",
        help="No Harmony Hub to hand -- listen for the remote's own traffic instead. Slower and "
        "needs the remote's buttons pressed repeatedly during the search; see pairing.sniff_network_address.",
    )
    pair.add_argument(
        "--verify-timeout",
        type=float,
        default=20.0,
        metavar="SECONDS",
        help="With --without-hub, time given to confirm the found address against real traffic (default: 20).",
    )

    listen = sub.add_parser("listen", help="Print button events live.")
    _add_listen_args(listen)
    listen.add_argument("--buttons", default=DEFAULT_PATH, help=f"Button profile JSON to name presses (default: {DEFAULT_PATH}).")

    capture = sub.add_parser("capture", help="Record raw packets to a JSONL file for offline analysis.")
    _add_listen_args(capture)
    capture.add_argument("--out", required=True, metavar="PATH", help="JSONL file to append to.")
    capture.add_argument("--seconds", type=float, default=15, help="How long to record (default: 15).")
    capture.add_argument("--note", default="", help="Label for this session, e.g. the button being pressed.")

    analyze = sub.add_parser("analyze", help="Summarise one or more capture files. Needs no hardware.")
    analyze.add_argument("files", nargs="+", help="JSONL files written by `capture`.")

    learn = sub.add_parser("learn", help="Record a button's signatures into the profile file. Needs no hardware.")
    learn.add_argument("--from", dest="source", required=True, help="Capture JSONL to take signatures from.")
    learn.add_argument("--key", help="Stable identifier, e.g. cursor_up. Required unless --all.")
    learn.add_argument("--label", help="Human-readable name, e.g. 'Move Cursor Up'. Required unless --all.")
    learn.add_argument(
        "--all",
        action="store_true",
        help="Take every distinct button in the capture and name it from its HID usage. "
        "For sweeping a whole remote in one session instead of one button at a time.",
    )
    learn.add_argument("--buttons", default=DEFAULT_PATH, help=f"Profile file to update (default: {DEFAULT_PATH}).")

    scan = sub.add_parser("scan", help="Ping all 12 channels repeatedly and report which ones answer.")
    _add_radio_args(scan)
    scan.add_argument("--address", metavar="HEX", required=True, help="The 5-byte network address, e.g. 17129BFCB6.")
    scan.add_argument("--rounds", type=int, default=5, help="Sweeps of all 12 channels (default: 5).")

    energy = sub.add_parser(
        "energy",
        help="Detect raw RF energy per channel, ignoring addresses entirely. "
        "Use when capture returns nothing and you need to know whether the remote is transmitting at all.",
    )
    _add_radio_args(energy)
    energy.add_argument("--address", metavar="HEX", required=True, help="The 5-byte network address.")
    energy.add_argument("--seconds", type=float, default=20, help="Measurement duration (default: 20).")

    bench = sub.add_parser("benchmark", help="Measure how fast the FT232H link can poll the radio.")
    _add_radio_args(bench)
    bench.add_argument("--seconds", type=float, default=3, help="Measurement duration (default: 3).")

    return parser


def _radio(args: argparse.Namespace) -> Optional[Any]:
    try:
        return create_radio(args.csn_pin, args.ce_pin)
    except Exception as err:
        logger.error("Failed to initialize the FT232H/nRF24L01+ hardware: %s", err)
        return None


def _start_channel(receiver: HarmonyReceiver, args: argparse.Namespace) -> tuple[Optional[int], float]:
    """Picks the channel to start on, and how long it gets to prove itself.

    An explicit `--channel` is the operator's own conclusion, so it is
    `scan`), so it is trusted indefinitely: dropping it because the remote
    happened to be idle for the first few seconds would throw away better
    information than the sweep can recover on its own.
    """
    if args.channel is not None:
        return args.channel, args.probe_interval
    if args.probe:
        channel = receiver.probe_for_channel()
        if channel is None:
            logger.warning("The probe was inconclusive; falling back to a passive channel search.")
        return channel, args.probe_interval
    return None, args.probe_interval


def cmd_pair(args: argparse.Namespace) -> int:
    radio = _radio(args)
    if radio is None:
        return 1

    try:
        if args.without_hub:
            logger.info("No Hub needed -- press buttons on the remote repeatedly while this searches...")
            network_address, channel = sniff_network_address(
                radio,
                timeout_sec=args.timeout,
                verify_timeout_sec=args.verify_timeout,
                on_progress=logger.info,
            )
        else:
            logger.info("Put the Harmony Hub into pairing mode (press its pair/reset button) now...")
            network_address, channel = discover_network_address(radio, timeout_sec=args.timeout)
    except PairingTimeout as err:
        logger.error("Pairing failed: %s", err)
        return 1

    logger.info("Network address %s, found on channel %d.", network_address.hex().upper(), channel)
    logger.info("Re-run with:  --address %s --channel %d", network_address.hex().upper(), channel)
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    radio = _radio(args)
    if radio is None:
        return 1

    buttons = ButtonMap.load(args.buttons)
    logger.info("Loaded %d known button(s) from %s.", len(buttons), args.buttons)

    network_address = bytes.fromhex(args.address)
    receiver = HarmonyReceiver(radio, network_address, resolve_label=buttons.resolve)
    channel, probe_interval = _start_channel(receiver, args)

    logger.info("Listening on %s... (Ctrl+C to stop)", network_address.hex().upper())
    try:
        for event in receiver.events(
            start_channel=channel, dwell=args.dwell,
            probe_interval=probe_interval, silent=not args.allow_ack,
        ):
            if event.kind == "repeat":
                logger.debug("%s", event)
            else:
                logger.info("%s", event)
    except KeyboardInterrupt:
        logger.info("Stopped.")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    radio = _radio(args)
    if radio is None:
        return 1

    network_address = bytes.fromhex(args.address)
    out = Path(args.out)

    with CaptureLog(out, note=args.note) as capture:
        receiver = HarmonyReceiver(radio, network_address, capture=capture)
        channel, probe_interval = _start_channel(receiver, args)

        logger.info("Recording %.0fs to %s%s...", args.seconds, out, f" ({args.note})" if args.note else "")
        deadline = time.monotonic() + args.seconds
        packets = 0
        try:
            for frame in receiver.sniff(
                start_channel=channel, dwell=args.dwell,
                probe_interval=probe_interval, silent=not args.allow_ack,
            ):
                if frame is not None:
                    packets += 1
                    logger.info(
                        "  %-8s %s%s", frame.kind, frame.payload.hex().upper(),
                        f"  signature={frame.signature}" if frame.signature else "",
                    )
                if time.monotonic() > deadline:
                    break
        except KeyboardInterrupt:
            logger.info("Interrupted.")

    logger.info("Captured %d packet(s) to %s (channel %s).", packets, out, receiver.locked_channel)
    if packets == 0:
        logger.warning("Nothing received. The Hub's channel may not have been found -- try --probe.")
    return 0


def _load_capture(path: Path) -> tuple[list[dict], list[dict]]:
    sessions, packets = [], []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("type") == "packet":
                packets.append(record)
            elif record.get("type") == "session":
                sessions.append(record)
    return sessions, packets


def cmd_analyze(args: argparse.Namespace) -> int:
    for name in args.files:
        path = Path(name)
        if not path.exists():
            logger.error("No such capture file: %s", path)
            continue

        sessions, packets = _load_capture(path)
        notes = ", ".join(s.get("note", "") for s in sessions if s.get("note")) or "(no note)"
        print(f"\n=== {path}  [{notes}]")
        print(f"    {len(packets)} packet(s) across {len(sessions)} session(s)")
        if not packets:
            continue

        kinds = Counter(p.get("kind") or "invalid" for p in packets)
        channels = Counter(p["channel"] for p in packets)
        print(f"    kinds:    {dict(kinds)}")
        print(f"    channels: {dict(channels)}")

        buttons: dict[str, list[float]] = defaultdict(list)
        releases = 0
        others: Counter = Counter()
        for packet in packets:
            frame = parse_frame(bytes.fromhex(packet["raw"]))
            if frame is None:
                continue
            if not frame.is_button:
                others[f"{frame.kind:<8} {packet['raw']}"] += 1
            elif frame.is_release:
                releases += 1
            else:
                buttons[f"{frame.signature}  {frame.name}"].append(packet["t"])

        if buttons:
            print(f"    BUTTONS PRESSED ({releases} release report(s) seen):")
            for label, times in sorted(buttons.items()):
                stamps = ", ".join(f"{t:.1f}s" for t in times[:6])
                more = f" (+{len(times) - 6} more)" if len(times) > 6 else ""
                print(f"      {label:<28} x{len(times):<3} at {stamps}{more}")
        else:
            print("    no button reports -- only status/tick traffic was seen.")

        if others:
            print("    non-button traffic:")
            for raw, count in others.most_common(8):
                print(f"      {raw}  x{count}")
    print()
    return 0


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")


def cmd_learn(args: argparse.Namespace) -> int:
    source = Path(args.source)
    if not source.exists():
        logger.error("No such capture file: %s", source)
        return 1

    _, packets = _load_capture(source)
    # Releases share a report id with the press but carry an empty body, and
    # status frames are not buttons at all; neither identifies what was held.
    found = {}
    for packet in packets:
        frame = parse_frame(bytes.fromhex(packet["raw"]))
        if frame is not None and frame.is_button and not frame.is_release:
            found.setdefault(frame.signature, frame)

    if not found:
        logger.error("%s contains no button reports; nothing to learn.", source)
        return 1

    buttons = ButtonMap.load(args.buttons)

    if args.all:
        for signature, frame in sorted(found.items()):
            # An unnamed usage still gets an entry: the signature is the real
            # key, and a placeholder label is easier to correct by hand later
            # than a missing row is to notice.
            label = frame.label or f"{frame.kind} 0x{frame.usage:04X}"
            key = _slug(label)
            buttons.learn(key, label, signature)
            logger.info("  %s  %-22s %s", signature, label, key)
        buttons.save(args.buttons)
        logger.info("Learned %d button(s) from %s; %d known in total.", len(found), source.name, len(buttons))
        return 0

    if not args.key or not args.label:
        logger.error("--key and --label are required unless --all is given.")
        return 1
    if len(found) > 1:
        # Two distinct buttons in one capture would silently teach the map a
        # wrong name, and there is no way to tell from here which press the
        # label belongs to. Better to stop than to poison the profile.
        logger.error(
            "%s contains %d distinct buttons (%s). Re-capture with one button, or use --all.",
            source, len(found), ", ".join(sorted(found)),
        )
        return 1

    signature = next(iter(found))
    existing = buttons.identify(signature)
    if existing is not None and existing.key != args.key:
        logger.error("Signature %s is already learned as '%s'.", signature, existing.label)
        return 1

    buttons.learn(args.key, args.label, signature)
    buttons.save(args.buttons)
    logger.info("Learned %s = %s (%s); %d button(s) known.", signature, args.label, args.key, len(buttons))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    radio = _radio(args)
    if radio is None:
        return 1

    receiver = HarmonyReceiver(radio, bytes.fromhex(args.address))
    logger.info("Pinging all %d channels x%d rounds...", len(HARMONY_CHANNELS), args.rounds)
    acks = receiver.scan_channels(rounds=args.rounds)

    for channel in HARMONY_CHANNELS:
        count = acks[channel]
        bar = "#" * count
        print(f"  channel {channel:>2}: {count}/{args.rounds} {bar}")

    answering = [c for c, n in acks.items() if n]
    print()
    if not answering:
        print("  No channel answered. The Hub may be off, out of range, or the address may be wrong.")
    elif len(answering) == len(HARMONY_CHANNELS):
        print("  Every channel answered -- ACK detection is meaningless here; ignore probe results.")
    else:
        print(f"  Answering: {answering}  -> the Hub is most likely on {max(answering, key=lambda c: acks[c])}")
    return 0


def cmd_energy(args: argparse.Namespace) -> int:
    radio = _radio(args)
    if radio is None:
        return 1

    receiver = HarmonyReceiver(radio, bytes.fromhex(args.address))
    logger.info("Measuring RF energy for %.0fs -- hold a button on the remote now.", args.seconds)
    hits = receiver.scan_energy(args.seconds)

    total = max(sum(hits.values()), 1)
    for channel in HARMONY_CHANNELS:
        share = 100 * hits[channel] / total
        print(f"  channel {channel:>2}: {hits[channel]:>5} samples  {share:5.1f}%  {'#' * int(share / 2)}")

    hot = [c for c in HARMONY_CHANNELS if hits[c] > total * 0.05]
    print()
    if not any(hits.values()):
        print("  No RF energy at all. The remote is not transmitting, or is out of range.")
    else:
        print(f"  Energy concentrated on: {hot} -- capture there.")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    radio = _radio(args)
    if radio is None:
        return 1

    set_silent(radio)
    radio.listen = True

    # Every one of these is a USB round trip on an FT232H, which is what
    # actually caps how fast channels can be swept -- worth knowing before
    # tuning dwell times against a 100ms packet cadence.
    rates = {}
    for label, action in (
        ("available()", lambda: radio.available()),
        ("channel write", lambda: setattr(radio, "channel", HARMONY_CHANNELS[0])),
    ):
        count = 0
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            action()
            count += 1
        rates[label] = count / args.seconds
        print(f"{label:>15}: {rates[label]:8.0f}/s   ({1000 / rates[label]:.2f} ms each)")

    sweep_ms = len(HARMONY_CHANNELS) * 1000 / rates["channel write"]
    print(f"\n  a bare 12-channel sweep costs at least {sweep_ms:.0f} ms in channel writes alone")
    print(f"  the remote sends a packet every ~100 ms while a button is held")
    return 0


COMMANDS = {
    "pair": cmd_pair,
    "listen": cmd_listen,
    "capture": cmd_capture,
    "analyze": cmd_analyze,
    "learn": cmd_learn,
    "scan": cmd_scan,
    "energy": cmd_energy,
    "benchmark": cmd_benchmark,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
