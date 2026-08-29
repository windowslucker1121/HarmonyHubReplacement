"""The one piece of the deploy machinery that an update never replaces.

Deployed once, by hand or by the bootstrap step of `harmony-deploy`, to
`$ROOT/bin/harmony-launch` -- outside every release directory. systemd (or
any supervisor) execs this instead of the hub directly. It decides which
release's code to boot, counts how many times a trial release has been
tried, and falls back to the previous one once it has failed too often --
then execs into the real hub, replacing itself entirely.

Stdlib only, no imports from this package or any dependency in the venv,
and no `from __future__ import annotations` cleverness either: whatever
Python built this file's copy in `$ROOT/bin` has to run it standalone, with
nothing else on `sys.path` guaranteed to still work. This copy inside
`harmony_hub.update` exists so it has proper test coverage; what actually
ships to `$ROOT/bin/harmony-launch` is a byte-for-byte copy of this file,
never an import of it.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

MAX_TRIAL_ATTEMPTS = 2
STATE_RELATIVE_PATH = os.path.join("data", "update_state.json")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read_state(state_path):
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_state(state_path, data):
    directory = os.path.dirname(state_path) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=os.path.basename(state_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, state_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def decide_release(root):
    """Updates trial bookkeeping and returns `(src_dir, state)` for the release to boot.

    `src_dir` is `None` when no release has ever been installed -- a fresh
    device with nothing deployed to it yet, which is not this function's
    problem to solve.

    A release counts as failed once it has been *tried* `MAX_TRIAL_ATTEMPTS`
    times without a confirmation being recorded (see `update/confirm.py`,
    which runs inside the hub process itself once it is actually serving) --
    at that point this function rolls back the bookkeeping to the release it
    replaced, rather than trying the bad one a third time.
    """
    root = os.path.abspath(str(root))
    state_path = os.path.join(root, STATE_RELATIVE_PATH)
    state = _read_state(state_path)
    trial = state.get("trial")

    if trial is not None:
        if trial.get("attempts", 0) >= MAX_TRIAL_ATTEMPTS:
            history = state.get("history") or []
            history.append({"build_id": trial.get("release"), "installed_at": _now(), "outcome": "rolled_back"})
            state["previous"] = state.get("current")
            state["current"] = trial.get("from")
            state["trial"] = None
            state["history"] = history[-50:]
        else:
            trial["attempts"] = trial.get("attempts", 0) + 1
            state["trial"] = trial

    _write_state(state_path, state)

    current = state.get("current")
    if not current:
        return None, state
    return os.path.join(root, "releases", current, "src"), state


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1:
        sys.stderr.write("usage: harmony-launch <root>\n")
        return 2

    root = os.path.abspath(argv[0])
    src_dir, state = decide_release(root)
    if src_dir is None:
        sys.stderr.write(f"harmony-launch: no release recorded under {root} -- deploy one first\n")
        return 1
    if not os.path.isdir(src_dir):
        sys.stderr.write(f"harmony-launch: release directory {src_dir} is missing\n")
        return 1

    venv_python = os.path.join(root, "venv", "bin", "python")
    if os.name == "nt":
        venv_python = os.path.join(root, "venv", "Scripts", "python.exe")
    python = venv_python if os.path.exists(venv_python) else sys.executable

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_dir + (os.pathsep + existing if existing else "")
    env["HARMONY_UPDATE_ROOT"] = root

    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    os.chdir(data_dir)

    os.execve(python, [python, "-m", "harmony_hub.server"], env)  # never returns on success


if __name__ == "__main__":
    raise SystemExit(main())
