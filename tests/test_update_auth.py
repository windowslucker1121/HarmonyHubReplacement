"""HMAC over a nonce and content hash -- the token itself never appears here either, on purpose."""

from __future__ import annotations

import stat
import sys

import pytest

from harmony_hub.update.auth import (
    InvalidSignature,
    fingerprint,
    generate_token,
    load_or_create_token,
    sign,
    verify,
)

CONTENT_HASH = "a" * 64


def test_a_correct_signature_verifies():
    token = generate_token()
    signature = sign(token, nonce=1, content_sha256=CONTENT_HASH)
    verify(token, nonce=1, content_sha256=CONTENT_HASH, signature=signature, last_nonce=0)


def test_the_wrong_token_is_rejected():
    token = generate_token()
    other = generate_token()
    signature = sign(token, nonce=1, content_sha256=CONTENT_HASH)
    with pytest.raises(InvalidSignature):
        verify(other, nonce=1, content_sha256=CONTENT_HASH, signature=signature, last_nonce=0)


def test_a_tampered_content_hash_is_rejected():
    token = generate_token()
    signature = sign(token, nonce=1, content_sha256=CONTENT_HASH)
    with pytest.raises(InvalidSignature):
        verify(token, nonce=1, content_sha256="b" * 64, signature=signature, last_nonce=0)


def test_a_replayed_nonce_is_rejected_even_with_a_correct_signature():
    token = generate_token()
    signature = sign(token, nonce=5, content_sha256=CONTENT_HASH)
    with pytest.raises(InvalidSignature):
        verify(token, nonce=5, content_sha256=CONTENT_HASH, signature=signature, last_nonce=5)
    with pytest.raises(InvalidSignature):
        verify(token, nonce=4, content_sha256=CONTENT_HASH, signature=signature, last_nonce=5)


def test_a_higher_nonce_than_last_seen_is_accepted():
    token = generate_token()
    signature = sign(token, nonce=6, content_sha256=CONTENT_HASH)
    verify(token, nonce=6, content_sha256=CONTENT_HASH, signature=signature, last_nonce=5)


def test_load_or_create_generates_a_token_on_first_use(tmp_path):
    path = tmp_path / "data" / "update_token"
    token = load_or_create_token(path)
    assert len(token) == 32
    assert path.exists()
    assert load_or_create_token(path) == token  # second call reads the same token back


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits do not apply on Windows")
def test_the_token_file_is_created_owner_only(tmp_path):
    path = tmp_path / "update_token"
    load_or_create_token(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_a_corrupt_token_file_is_reported_rather_than_silently_reused(tmp_path):
    path = tmp_path / "update_token"
    path.write_bytes(b"too short")
    with pytest.raises(ValueError):
        load_or_create_token(path)


def test_fingerprint_is_short_stable_and_does_not_reveal_the_token():
    token = generate_token()
    fp = fingerprint(token)
    assert len(fp) == 8
    assert fp == fingerprint(token)
    assert fp != fingerprint(generate_token())
    assert fp.encode() not in token  # trivially true for a hash, but the point stands
