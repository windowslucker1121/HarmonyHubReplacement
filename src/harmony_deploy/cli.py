"""harmony-deploy: push code updates to a running hub, or provision one from scratch over SSH.

    harmony-deploy push TARGET [--no-tests] [--no-web] [--force] [--dry-run]
    harmony-deploy rollback TARGET
    harmony-deploy version TARGET
    harmony-deploy setup TARGET [--host H] [--user U] [--root ~/harmony]
                         [--yes] [--dry-run] [--no-tests] [--no-web]
    harmony-deploy inspect [--host H] [--user U] [--root ~/harmony]
    harmony-deploy build [--out DIR] [--no-tests] [--no-web]

TARGET is a name from deploy_targets.json at the repo root (gitignored --
see deploy_targets.example.json for the shape), which maps a name to a URL
and a local copy of that device update token. The token lives only on the
device and in this file; it never crosses the network -- see
harmony_hub.update.auth.

push is the routine path: it assumes a hub is already running and healthy
enough to accept a signed HTTP request. setup is for everything push
cannot do -- a bare device with nothing on it yet, a hub that is down, a
token never fetched -- and writes the deploy_targets.json entry push needs
once it is done. Missing host/user/root are prompted for; missing
credentials try SSH keys first and only then ask for a password.

build produces the same bundle as push and setup start from, but only
writes it to disk -- alongside its manifest as its own `.manifest.json`
file, since a GitHub release asset has to be an actual file. This is what
`.github/workflows/release.yml` runs so a tagged release's assets are
built exactly the same way a manual push would build them; a device can
then fetch and install a release directly (see `harmony_hub.update.source`)
without anyone at a keyboard running `push`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from harmony_hub.update import auth as update_auth

from . import setup as setup_module
from .bundling import build_release_bundle
from .errors import DeployError
from .targets import load_targets, read_token, resolve_target
from .verify import wait_for_version

REPO_ROOT = Path(__file__).resolve().parents[2]


def push(
    repo_root: Path,
    target_name: str,
    *,
    run_tests_first: bool = True,
    build_web_first: bool = True,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    target = resolve_target(target_name, load_targets())
    manifest, tar_path = build_release_bundle(
        repo_root, run_tests_first=run_tests_first, build_web_first=build_web_first
    )

    if dry_run:
        print("dry-run: not pushing anything")
        return

    token = read_token(target["token_file"])
    nonce = int(time.time() * 1000)
    signature = update_auth.sign(token, nonce, manifest.content_sha256)

    url = target["url"].rstrip("/") + "/api/update"
    params = {"force": "true"} if force else {}

    print(f"Pushing to {target['url']} ...")
    with tar_path.open("rb") as f:
        response = httpx.post(
            url,
            params=params,
            data={"manifest": manifest.model_dump_json()},
            files={"bundle": (tar_path.name, f, "application/gzip")},
            headers={"X-Harmony-Nonce": str(nonce), "X-Harmony-Signature": signature},
            timeout=180.0,
        )
    if response.status_code >= 400:
        raise DeployError(f"device rejected the update ({response.status_code}): {response.text}")
    print(f"Accepted: {response.json()}")
    print(f"Waiting for {target['url']} to come back on {manifest.build_id} ...")
    wait_for_version(target["url"], manifest.build_id)


def _prompt(label: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def main(argv: Optional["list[str]"] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harmony-deploy", description="Push code updates to a running hub, or provision one over SSH."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    push_p = sub.add_parser("push", help="Build a bundle and push it to a target over HTTP.")
    push_p.add_argument("target")
    push_p.add_argument("--no-tests", action="store_true", help="Skip pytest -q before building.")
    push_p.add_argument("--no-web", action="store_true", help="Skip flutter build web; reuse app/build/web.")
    push_p.add_argument("--force", action="store_true", help="Update even while a scene is active on the device.")
    push_p.add_argument("--dry-run", action="store_true", help="Build the bundle but do not push it anywhere.")

    rollback_p = sub.add_parser("rollback", help="Roll a target back to its previous release.")
    rollback_p.add_argument("target")

    version_p = sub.add_parser("version", help="Show what a target is currently running.")
    version_p.add_argument("target")

    setup_p = sub.add_parser(
        "setup", help="Provision or update one device over SSH -- for a bare device, or a hub that is down."
    )
    setup_p.add_argument("target", help="Name to record this device under in deploy_targets.json.")
    setup_p.add_argument("--host", help="Hostname or IP. Prompted for if omitted.")
    setup_p.add_argument("--user", help="SSH username. Prompted for if omitted (default: pi).")
    setup_p.add_argument("--root", help="Install root on the device. Prompted for if omitted (default: ~/harmony).")
    setup_p.add_argument("--port", type=int, default=22)
    setup_p.add_argument("--no-tests", action="store_true", help="Skip pytest -q before building.")
    setup_p.add_argument("--no-web", action="store_true", help="Skip flutter build web; reuse app/build/web.")
    setup_p.add_argument("--yes", action="store_true", help="Do not ask for confirmation before changing the device.")
    setup_p.add_argument(
        "--dry-run", action="store_true", help="Connect, inspect, and print the plan; change nothing."
    )

    inspect_p = sub.add_parser("inspect", help="Read-only: report a device's state without changing anything.")
    inspect_p.add_argument("--host", help="Hostname or IP. Prompted for if omitted.")
    inspect_p.add_argument("--user", help="SSH username. Prompted for if omitted (default: pi).")
    inspect_p.add_argument("--root", help="Install root on the device. Prompted for if omitted (default: ~/harmony).")
    inspect_p.add_argument("--port", type=int, default=22)

    build_p = sub.add_parser(
        "build", help="Build a bundle and its manifest.json to disk, without pushing it anywhere."
    )
    build_p.add_argument("--out", help="Directory to write the bundle to (default: .harmony-deploy).")
    build_p.add_argument("--no-tests", action="store_true", help="Skip pytest -q before building.")
    build_p.add_argument("--no-web", action="store_true", help="Skip flutter build web; reuse app/build/web.")

    args = parser.parse_args(argv)
    try:
        if args.command == "push":
            push(
                REPO_ROOT,
                args.target,
                run_tests_first=not args.no_tests,
                build_web_first=not args.no_web,
                force=args.force,
                dry_run=args.dry_run,
            )
        elif args.command == "rollback":
            target = resolve_target(args.target, load_targets())
            response = httpx.post(target["url"].rstrip("/") + "/api/update/rollback", timeout=30.0)
            if response.status_code >= 400:
                raise DeployError(f"rollback rejected ({response.status_code}): {response.text}")
            print(response.json())
        elif args.command == "version":
            target = resolve_target(args.target, load_targets())
            response = httpx.get(target["url"].rstrip("/") + "/api/version", timeout=10.0)
            response.raise_for_status()
            print(json.dumps(response.json(), indent=2))
        elif args.command == "setup":
            host = args.host or _prompt("Host")
            user = args.user or _prompt("Username", default="pi")
            root = args.root or _prompt("Install root", default="~/harmony")
            setup_module.run_setup(
                REPO_ROOT,
                args.target,
                host=host,
                user=user,
                root=root,
                port=args.port,
                run_tests_first=not args.no_tests,
                build_web_first=not args.no_web,
                dry_run=args.dry_run,
                assume_yes=args.yes,
            )
        elif args.command == "inspect":
            host = args.host or _prompt("Host")
            user = args.user or _prompt("Username", default="pi")
            root = args.root or _prompt("Install root", default="~/harmony")
            state = setup_module.run_inspect(host, user, root, port=args.port)
            setup_module.print_state(state)
        elif args.command == "build":
            build_release_bundle(
                REPO_ROOT,
                run_tests_first=not args.no_tests,
                build_web_first=not args.no_web,
                out_dir=Path(args.out) if args.out else None,
                write_manifest_json=True,
            )
    except DeployError as err:
        print(f"harmony-deploy: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
