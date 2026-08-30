"""git_info, run_tests, and build_web -- the external-process bits of building a bundle.

No real git/pytest/flutter invocation here except git_info's own graceful
fallback path (git itself is assumed present in CI, same as everywhere
else in this project). subprocess.run and shutil.which are faked so these
stay fast and platform-independent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harmony_deploy.bundling import build_release_bundle, build_web, git_info, run_tests
from harmony_deploy.errors import DeployError
from harmony_hub.update.manifest import Manifest


def test_git_info_outside_a_checkout_returns_empty_rather_than_raising(tmp_path):
    # tmp_path is never a git checkout -- `git rev-parse` there fails, which
    # must not stop a build; it should just mean "no git metadata available".
    sha, dirty = git_info(tmp_path)
    assert sha == ""
    assert dirty is False


def test_run_tests_raises_on_a_failing_suite(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    with pytest.raises(DeployError, match="tests failed"):
        run_tests(tmp_path)


def test_run_tests_is_silent_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    run_tests(tmp_path)  # must not raise


def test_build_web_resolves_flutter_to_a_real_path_rather_than_the_bare_name(tmp_path, monkeypatch):
    """The regression this guards: `subprocess.run(["flutter", ...])` silently fails on Windows,

    where the real executable is `flutter.bat` and `CreateProcess` (what
    `subprocess` uses when `shell=False`) does not search `PATHEXT` the way
    typing the command into an interactive shell does. Only `shutil.which`
    -- or a shell -- actually finds it there. Resolving the path explicitly
    before calling `subprocess.run` is what fixes it; this pins that down
    so it cannot quietly regress back to the bare name.
    """
    seen = {}

    def fake_which(name):
        assert name == "flutter"
        return "C:\flutter\bin\flutter.BAT"

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("harmony_deploy.bundling.shutil.which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)

    build_web(tmp_path, "build-1")

    assert seen["cmd"][0] == "C:\flutter\bin\flutter.BAT"
    assert seen["cmd"][0] != "flutter"  # the bare name is exactly what must never be passed on Windows


def test_build_web_raises_a_clear_error_when_flutter_is_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr("harmony_deploy.bundling.shutil.which", lambda name: None)
    with pytest.raises(DeployError, match="not on PATH"):
        build_web(tmp_path, "build-1")


def test_build_web_raises_on_a_failed_build(tmp_path, monkeypatch):
    monkeypatch.setattr("harmony_deploy.bundling.shutil.which", lambda name: "/usr/bin/flutter")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    with pytest.raises(DeployError, match="flutter build web failed"):
        build_web(tmp_path, "build-1")


def _seed_minimal_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "fake"\ndependencies = ["httpx>=0.27"]\n', encoding="utf-8")
    src_hub = root / "src" / "harmony_hub"
    src_hub.mkdir(parents=True)
    (src_hub / "__init__.py").write_text("", encoding="utf-8")
    src_receiver = root / "src" / "harmony_receiver"
    src_receiver.mkdir(parents=True)
    (src_receiver / "__init__.py").write_text("", encoding="utf-8")
    return root


def test_build_release_bundle_writes_bundle_and_manifest_json_to_out_dir(tmp_path):
    """This is what `harmony-deploy build` -- and so CI's release job -- relies on:

    a `manifest.json` file next to the tar, since a GitHub release asset
    has to be an actual file, not a value only ever held in memory.
    """
    repo = _seed_minimal_repo(tmp_path / "repo")
    out_dir = tmp_path / "dist"

    manifest, tar_path = build_release_bundle(
        repo, run_tests_first=False, build_web_first=False, out_dir=out_dir, write_manifest_json=True
    )

    assert tar_path.parent == out_dir
    assert tar_path.exists()
    manifest_path = out_dir / f"{manifest.build_id}.manifest.json"
    assert manifest_path.exists()
    restored = Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    assert restored == manifest


def test_build_release_bundle_defaults_skip_manifest_json_and_use_dot_harmony_deploy(tmp_path):
    """The default behaviour (what `push`/`setup` use) must stay unchanged: no stray manifest.json file."""
    repo = _seed_minimal_repo(tmp_path / "repo")

    manifest, tar_path = build_release_bundle(repo, run_tests_first=False, build_web_first=False)

    assert tar_path.parent == repo / ".harmony-deploy"
    assert not (repo / ".harmony-deploy" / f"{manifest.build_id}.manifest.json").exists()
