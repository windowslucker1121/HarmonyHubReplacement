"""Verify, stage, install dependencies, smoke-test, activate -- in that order.

The happy-path test builds a real bundle from this project's own source (via
`harmony_hub.update.bundle`) and installs it against a fake root, so the
smoke test genuinely imports the real `harmony_hub`/`harmony_receiver`
packages rather than a hand-rolled stand-in. Dependencies are pre-recorded
as already installed (`deps_hash` seeded to match) so this never touches
pip or the network -- this project's own tests never do.

The failure-path tests use small fake repos instead, so a deliberately
broken bundle can be built without touching the real source tree.
"""

from __future__ import annotations

import types

import harmony_hub
import pytest

from harmony_hub.events import EventBroker
from harmony_hub.update import state as state_module
from harmony_hub.update.bundle import build_bundle, hash_requirements, read_requirements
from harmony_hub.update.installer import InstallError, check_disk_space, install
from harmony_hub.update.manifest import Manifest

REPO_ROOT = harmony_hub.__file__ and __import__("pathlib").Path(harmony_hub.__file__).resolve().parents[2]


def _seed_broken_repo(root, *, broken_server=False):
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\ndependencies = []\n', encoding="utf-8")

    src_hub = root / "src" / "harmony_hub"
    src_hub.mkdir(parents=True)
    (src_hub / "__init__.py").write_text("", encoding="utf-8")
    if broken_server:
        (src_hub / "server.py").write_text("raise ImportError('boom -- deliberately broken for a test')\n", encoding="utf-8")
    else:
        (src_hub / "server.py").write_text("def main(): return 0\n", encoding="utf-8")

    src_receiver = root / "src" / "harmony_receiver"
    src_receiver.mkdir(parents=True)
    (src_receiver / "__init__.py").write_text("", encoding="utf-8")
    (src_receiver / "receiver.py").write_text("def run(): return 0\n", encoding="utf-8")
    return root


@pytest.fixture
def root(tmp_path):
    release_root = tmp_path / "device_root"
    release_root.mkdir()
    return release_root


def test_disk_space_is_checked_before_anything_is_written(root, monkeypatch):
    monkeypatch.setattr(
        "harmony_hub.update.installer.shutil.disk_usage",
        lambda path: types.SimpleNamespace(total=0, used=0, free=1),
    )
    manifest = Manifest(
        build_id="x", created_at="now", deps_hash="x", file_count=1, byte_count=10_000, content_sha256="a" * 64
    )
    with pytest.raises(InstallError, match="free"):
        check_disk_space(root, manifest.byte_count)


async def test_a_future_manifest_schema_is_refused_before_staging(root, tmp_path):
    manifest = Manifest(
        schema_version=999, build_id="x", created_at="now", file_count=0, byte_count=0, content_sha256="a" * 64
    )
    with pytest.raises(InstallError, match="update the hub itself"):
        await install(root, tmp_path / "irrelevant.tar.gz", manifest)
    assert state_module.list_releases(root) == []


async def test_a_build_id_already_installed_is_refused(root, tmp_path):
    state_module.release_dir(root, "dup-build").mkdir(parents=True)
    manifest = Manifest(build_id="dup-build", created_at="now", file_count=0, byte_count=0, content_sha256="a" * 64)
    with pytest.raises(InstallError, match="already installed"):
        await install(root, tmp_path / "irrelevant.tar.gz", manifest)


async def test_a_bundle_that_fails_extraction_activates_nothing(root, tmp_path):
    fake_repo = _seed_broken_repo(tmp_path / "repo")
    tar_path = tmp_path / "bundle.tar.gz"
    manifest = build_bundle(fake_repo, tar_path, build_id="build-1")

    # Corrupt the tar after the manifest was computed from the good version,
    # so its own content hash still matches (the API layer checks that
    # separately) but the tar itself is now garbage tarfile can't open.
    tar_path.write_bytes(b"not actually a tar file")

    with pytest.raises(InstallError):
        await install(root, tar_path, manifest)
    assert state_module.list_releases(root) == []


async def test_a_release_that_fails_its_smoke_test_is_removed_and_never_activated(root, tmp_path):
    broken_repo = _seed_broken_repo(tmp_path / "broken_repo", broken_server=True)
    tar_path = tmp_path / "bundle.tar.gz"
    manifest = build_bundle(broken_repo, tar_path, build_id="build-broken")

    state_module.save(state_module.UpdateState(deps_hash=manifest.deps_hash), state_module.release_dir(root, "").parent.parent / "data" / "update_state.json")

    with pytest.raises(InstallError, match="boom"):
        await install(root, tar_path, manifest)

    assert state_module.list_releases(root) == []
    final_state = state_module.load(root / "data" / "update_state.json")
    assert final_state.current is None


async def test_a_working_bundle_from_this_projects_own_source_installs_and_activates(root, tmp_path):
    tar_path = tmp_path / "bundle.tar.gz"
    manifest = build_bundle(REPO_ROOT, tar_path, build_id="build-real")

    # Dependencies are already satisfied in this test environment; recording
    # the matching hash up front skips `pip install` so this test touches no
    # network, consistent with the rest of this project's test suite.
    state_module.save(
        state_module.UpdateState(deps_hash=manifest.deps_hash), root / "data" / "update_state.json"
    )

    broker = EventBroker()
    new_state = await install(root, tar_path, manifest, broker=broker, keep_releases=2)

    assert new_state.current == "build-real"
    assert (root / "releases" / "build-real" / "src" / "harmony_hub" / "server.py").is_file()
    assert (root / "releases" / "build-real" / "manifest.json").is_file()

    events = [e.detail for e in broker.history if e.type == "update"]
    assert any("Smoke test passed" in text for text in events)
    assert any("activated" in text for text in events)


async def test_a_second_install_prunes_old_releases_beyond_the_keep_count(root, tmp_path):
    tar_path = tmp_path / "bundle.tar.gz"
    manifest_1 = build_bundle(REPO_ROOT, tar_path, build_id="build-a")
    state_module.save(
        state_module.UpdateState(deps_hash=manifest_1.deps_hash), root / "data" / "update_state.json"
    )
    await install(root, tar_path, manifest_1, keep_releases=1)

    tar_path_2 = tmp_path / "bundle2.tar.gz"
    manifest_2 = build_bundle(REPO_ROOT, tar_path_2, build_id="build-b")
    new_state = await install(root, tar_path_2, manifest_2, keep_releases=1)

    assert new_state.current == "build-b"
    assert new_state.previous == "build-a"
    # keep=1 with current+previous already covering the newest release means
    # nothing older survives beyond those two.
    assert state_module.list_releases(root) == ["build-a", "build-b"]
