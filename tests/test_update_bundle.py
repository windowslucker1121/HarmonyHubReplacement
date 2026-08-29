"""The allowlist is the whole guarantee that "config is not transferred" holds.

These tests build a bundle from a fake repo tree seeded with exactly the
files that must never ship -- hub_config.json, hub_settings.json,
buttons.json, credentials/, venv/ -- and check none of them make it in.
"""

from __future__ import annotations

import hashlib
import tarfile

import pytest

from harmony_hub.update.bundle import build_bundle, hash_requirements, make_build_id, read_requirements
from harmony_hub.update.manifest import is_allowed


def _seed_fake_repo(root):
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fake"\ndependencies = ["httpx>=0.27", "pydantic>=2.9"]\n', encoding="utf-8"
    )

    src_hub = root / "src" / "harmony_hub"
    src_hub.mkdir(parents=True)
    (src_hub / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    (src_hub / "server.py").write_text("def main(): return 0\n", encoding="utf-8")

    backends = src_hub / "backends"
    backends.mkdir()
    (backends / "__init__.py").write_text("", encoding="utf-8")
    (backends / "virtual.py").write_text("class Virtual: ...\n", encoding="utf-8")

    update_pkg = src_hub / "update"
    update_pkg.mkdir()
    (update_pkg / "__init__.py").write_text("", encoding="utf-8")

    src_receiver = root / "src" / "harmony_receiver"
    src_receiver.mkdir(parents=True)
    (src_receiver / "__init__.py").write_text("", encoding="utf-8")
    (src_receiver / "receiver.py").write_text("def run(): return 0\n", encoding="utf-8")

    # Everything below this line must never appear in a bundle.
    (root / "hub_config.json").write_text('{"devices": []}', encoding="utf-8")
    (root / "hub_settings.json").write_text('{"source": "radio", "address": "17129BFCB6"}', encoding="utf-8")
    (root / "buttons.json").write_text('{"power": {"label": "Power", "signatures": []}}', encoding="utf-8")

    credentials = root / "credentials"
    credentials.mkdir()
    (credentials / "androidtv_shieldtv.key").write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")

    venv = root / "venv" / "Lib" / "site-packages"
    venv.mkdir(parents=True)
    (venv / "some_dependency.py").write_text("# third-party\n", encoding="utf-8")

    pycache = src_hub / "__pycache__"
    pycache.mkdir()
    (pycache / "server.cpython-313.pyc").write_bytes(b"\x00\x01")

    # A config file that ended up *inside* src/ by mistake must still be
    # excluded -- this is what `manifest.DENY_NAMES` is for, independent of
    # the allowlist matching by directory.
    (src_hub / "hub_config.json").write_text("{}", encoding="utf-8")

    return root


@pytest.fixture
def fake_repo(tmp_path):
    return _seed_fake_repo(tmp_path / "repo")


def test_config_and_secrets_never_appear_in_a_bundle(fake_repo, tmp_path):
    output = tmp_path / "bundle.tar.gz"
    manifest = build_bundle(fake_repo, output, build_id="test-build")

    with tarfile.open(output, "r:gz") as tar:
        names = tar.getnames()

    forbidden_substrings = ["hub_config.json", "hub_settings.json", "buttons.json", "credentials", "venv", ".pyc"]
    for name in names:
        for forbidden in forbidden_substrings:
            assert forbidden not in name, f"{name!r} should never be in a bundle"

    assert "src/harmony_hub/server.py" in names
    assert "src/harmony_hub/backends/virtual.py" in names
    assert "src/harmony_receiver/receiver.py" in names
    assert "requirements.txt" in names
    assert manifest.file_count == len(names)


def test_the_web_build_is_spliced_in_under_the_packaged_ui_path(fake_repo, tmp_path):
    web_dir = tmp_path / "flutter_web_output"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (web_dir / "main.dart.js").write_text("// compiled", encoding="utf-8")

    output = tmp_path / "bundle.tar.gz"
    build_bundle(fake_repo, output, build_id="test-build", web_dir=web_dir, web_build_id="test-build")

    with tarfile.open(output, "r:gz") as tar:
        names = set(tar.getnames())

    assert "src/harmony_hub/web/index.html" in names
    assert "src/harmony_hub/web/main.dart.js" in names


def test_a_web_dir_that_does_not_exist_is_refused_rather_than_silently_skipped(fake_repo, tmp_path):
    with pytest.raises(FileNotFoundError):
        build_bundle(fake_repo, tmp_path / "bundle.tar.gz", build_id="x", web_dir=tmp_path / "nope")


def test_content_hash_matches_an_independent_recomputation(fake_repo, tmp_path):
    output = tmp_path / "bundle.tar.gz"
    manifest = build_bundle(fake_repo, output, build_id="test-build")

    expected = hashlib.sha256(output.read_bytes()).hexdigest()
    assert manifest.content_sha256 == expected


def test_manifest_round_trips_through_json(fake_repo, tmp_path):
    manifest = build_bundle(fake_repo, tmp_path / "bundle.tar.gz", build_id="test-build", git_sha="abc1234")
    restored = manifest.model_validate_json(manifest.model_dump_json())
    assert restored == manifest


def test_deps_hash_is_stable_regardless_of_requirements_ordering():
    a = hash_requirements("httpx>=0.27\npydantic>=2.9\n")
    b = hash_requirements("pydantic>=2.9\nhttpx>=0.27\n")
    assert a == b


def test_requirements_come_from_pyproject_dependencies(fake_repo):
    text = read_requirements(fake_repo / "pyproject.toml")
    assert "httpx>=0.27" in text
    assert "pydantic>=2.9" in text


def test_build_id_is_sortable_and_reflects_dirty_state():
    clean = make_build_id("abc1234", git_dirty=False)
    dirty = make_build_id("abc1234", git_dirty=True)
    assert clean.endswith("abc1234")
    assert dirty.endswith("abc1234-dirty")


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/harmony_hub/server.py", True),
        ("src/harmony_hub/backends/virtual.py", True),
        ("requirements.txt", True),
        ("src/harmony_hub/hub_config.json", False),
        ("hub_settings.json", False),
        ("credentials/androidtv_shieldtv.key", False),
        ("venv/Lib/site-packages/foo.py", False),
        ("src/harmony_hub/__pycache__/server.cpython-313.pyc", False),
        ("../../etc/passwd", False),
        ("src/harmony_hub/../../../etc/passwd", False),
    ],
)
def test_is_allowed_matches_the_allowlist(path, expected):
    assert is_allowed(path) is expected
