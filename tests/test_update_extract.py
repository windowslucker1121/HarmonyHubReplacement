"""Extraction assumes the tar is hostile even though its signature already checked out.

Each test builds a tar by hand -- not through `bundle.build_bundle`, which
would never produce any of these on purpose -- to prove `safe_extract`
rejects it before writing anything outside the staging directory.
"""

from __future__ import annotations

import io
import tarfile

import pytest

from harmony_hub.update.extract import UnsafeBundle, safe_extract


def _make_tar(path, members):
    """`members` is `[(name, bytes)]` for regular files, or `(name, None, kind)` for special entries."""
    with tarfile.open(path, "w:gz") as tar:
        for entry in members:
            if len(entry) == 2:
                name, data = entry
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                name, _, kind = entry
                info = tarfile.TarInfo(name=name)
                if kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                elif kind == "hardlink":
                    info.type = tarfile.LNKTYPE
                    info.linkname = "requirements.txt"
                elif kind == "dir":
                    info.type = tarfile.DIRTYPE
                tar.addfile(info)
    return path


@pytest.fixture
def dest(tmp_path):
    out = tmp_path / "staged"
    out.mkdir()
    return out


def test_a_well_formed_bundle_extracts_cleanly(tmp_path, dest):
    tar_path = _make_tar(
        tmp_path / "good.tar.gz",
        [
            ("requirements.txt", b"httpx>=0.27\n"),
            ("src/harmony_hub/server.py", b"def main(): return 0\n"),
            ("src/harmony_hub/backends/virtual.py", b"class Virtual: ...\n"),
        ],
    )
    count = safe_extract(tar_path, dest)
    assert count == 3
    assert (dest / "src" / "harmony_hub" / "server.py").read_bytes() == b"def main(): return 0\n"


@pytest.mark.parametrize(
    "bad_name",
    [
        "../../etc/passwd",
        "src/../../../etc/passwd",
        "/etc/passwd",
        "src/harmony_hub/not_on_the_allowlist.json",
        "hub_config.json",
    ],
)
def test_path_traversal_and_off_allowlist_members_are_rejected(tmp_path, dest, bad_name):
    tar_path = _make_tar(tmp_path / "evil.tar.gz", [(bad_name, b"pwned")])
    with pytest.raises(UnsafeBundle):
        safe_extract(tar_path, dest)
    assert list(dest.iterdir()) == []


def test_symlinks_are_rejected(tmp_path, dest):
    tar_path = _make_tar(tmp_path / "evil.tar.gz", [("src/harmony_hub/server.py", None, "symlink")])
    with pytest.raises(UnsafeBundle, match="links"):
        safe_extract(tar_path, dest)


def test_hardlinks_are_rejected(tmp_path, dest):
    tar_path = _make_tar(
        tmp_path / "evil.tar.gz",
        [("requirements.txt", b"x\n"), ("src/harmony_hub/server.py", None, "hardlink")],
    )
    with pytest.raises(UnsafeBundle, match="links"):
        safe_extract(tar_path, dest)


def test_an_oversized_member_is_rejected_without_writing_it(tmp_path, dest, monkeypatch):
    import harmony_hub.update.extract as extract_module

    monkeypatch.setattr(extract_module, "MAX_MEMBER_BYTES", 10)
    tar_path = _make_tar(tmp_path / "huge.tar.gz", [("requirements.txt", b"x" * 1000)])
    with pytest.raises(UnsafeBundle, match="exceeds the per-file limit"):
        safe_extract(tar_path, dest)
    assert list(dest.iterdir()) == []


def test_too_many_members_is_rejected(tmp_path, dest, monkeypatch):
    import harmony_hub.update.extract as extract_module

    monkeypatch.setattr(extract_module, "MAX_MEMBERS", 2)
    tar_path = _make_tar(
        tmp_path / "many.tar.gz",
        [("requirements.txt", b"x"), ("src/harmony_hub/server.py", b"x"), ("src/harmony_hub/config.py", b"x")],
    )
    with pytest.raises(UnsafeBundle, match="member limit"):
        safe_extract(tar_path, dest)


def test_refuses_to_extract_into_a_nonempty_directory(tmp_path, dest):
    (dest / "leftover.txt").write_text("from a previous attempt", encoding="utf-8")
    tar_path = _make_tar(tmp_path / "good.tar.gz", [("requirements.txt", b"x\n")])
    with pytest.raises(UnsafeBundle, match="not empty"):
        safe_extract(tar_path, dest)


def test_refuses_a_destination_that_does_not_exist(tmp_path):
    tar_path = _make_tar(tmp_path / "good.tar.gz", [("requirements.txt", b"x\n")])
    with pytest.raises(UnsafeBundle):
        safe_extract(tar_path, tmp_path / "does_not_exist")
