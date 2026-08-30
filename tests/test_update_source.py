"""fetch_latest/download against a fake GitHub, no network in sight.

`httpx.MockTransport` stands in for GitHub's REST API and its release
asset host -- both are plain HTTPS GETs from this module's point of view,
so a handler function is enough to cover every path: a real release, a
release missing an asset the release workflow is supposed to attach, an
unparseable manifest, and no network at all.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from harmony_hub.update.manifest import Manifest
from harmony_hub.update.source import (
    AvailableRelease,
    ReleaseFeedError,
    download,
    fetch_latest,
    is_newer,
)

REPO = "windowslucker1121/HarmonyHubReplacement"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def _manifest_json(build_id: str = "20260830T120000-abc1234") -> bytes:
    manifest = Manifest(
        build_id=build_id,
        git_sha="abc1234",
        created_at="2026-08-30T12:00:00+00:00",
        file_count=3,
        byte_count=100,
        content_sha256="a" * 64,
    )
    return manifest.model_dump_json().encode("utf-8")


def _release_payload(*, tag="v1.2.3", assets=None) -> dict:
    return {
        "tag_name": tag,
        "published_at": "2026-08-30T12:05:00Z",
        "body": "Release notes",
        "assets": assets if assets is not None else [],
    }


def _client(handler) -> httpx.AsyncClient:
    """`follow_redirects=True` matters here: it mirrors what `fetch_latest`
    and `download` each build for themselves by default, since GitHub always
    serves a release asset's `browser_download_url` as a redirect, never the
    file directly -- see `test_fetch_latest_follows_a_redirected_asset_url`.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


async def test_fetch_latest_happy_path():
    manifest_bytes = _manifest_json()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == LATEST_URL:
            return httpx.Response(
                200,
                json=_release_payload(
                    assets=[
                        {"name": "harmony-hub-x.tar.gz", "browser_download_url": "https://gh.example/bundle.tar.gz", "size": 12345},
                        {"name": "harmony-hub-x.manifest.json", "browser_download_url": "https://gh.example/manifest.json", "size": 200},
                    ]
                ),
            )
        if str(request.url) == "https://gh.example/manifest.json":
            return httpx.Response(200, content=manifest_bytes)
        raise AssertionError(f"unexpected request to {request.url}")

    async with _client(handler) as client:
        release = await fetch_latest(REPO, client=client)

    assert isinstance(release, AvailableRelease)
    assert release.tag == "v1.2.3"
    assert release.tar_url == "https://gh.example/bundle.tar.gz"
    assert release.tar_bytes == 12345
    assert release.build_id == "20260830T120000-abc1234"


async def test_fetch_latest_follows_a_redirected_asset_url():
    """Regression test: a real GitHub release asset's `browser_download_url`

    is always a 302 to signed, time-limited blob storage -- never the file
    itself. Every other test here (and, before this one existed, the whole
    test suite) used a handler that returned the manifest directly, so
    nothing ever caught `fetch_latest` failing to follow it -- a real
    `harmony-deploy build`-published release did, with
    `raise_for_status()` treating the unfollowed redirect as an error.
    """
    manifest_bytes = _manifest_json()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == LATEST_URL:
            return httpx.Response(
                200,
                json=_release_payload(
                    assets=[
                        {"name": "x.tar.gz", "browser_download_url": "https://gh.example/t.tar.gz"},
                        {
                            "name": "x.manifest.json",
                            "browser_download_url": "https://gh.example/redirect/manifest.json",
                        },
                    ]
                ),
            )
        if str(request.url) == "https://gh.example/redirect/manifest.json":
            return httpx.Response(302, headers={"location": "https://blob.example/signed-manifest.json"})
        if str(request.url) == "https://blob.example/signed-manifest.json":
            return httpx.Response(200, content=manifest_bytes)
        raise AssertionError(f"unexpected request to {request.url}")

    async with _client(handler) as client:
        release = await fetch_latest(REPO, client=client)

    assert release.build_id == "20260830T120000-abc1234"


async def test_fetch_latest_with_no_client_given_builds_one_that_follows_redirects(monkeypatch):
    """Same requirement as the test above, but exercised through

    `fetch_latest`'s own default client construction (`client=None`) rather
    than a client the test builds -- proving the constructor call itself
    passes `follow_redirects=True`. Subclassing the real `AsyncClient` and
    injecting a `MockTransport` keeps this off the real network.
    """
    captured_kwargs = {}

    class RecordingClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(404))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)

    with pytest.raises(ReleaseFeedError, match="no published releases"):
        await fetch_latest(REPO)

    assert captured_kwargs.get("follow_redirects") is True


async def test_fetch_latest_raises_when_repo_has_no_releases():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with _client(handler) as client:
        with pytest.raises(ReleaseFeedError, match="no published releases"):
            await fetch_latest(REPO, client=client)


async def test_fetch_latest_raises_when_bundle_asset_is_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_release_payload(
                assets=[{"name": "harmony-hub-x.manifest.json", "browser_download_url": "https://gh.example/m.json"}]
            ),
        )

    async with _client(handler) as client:
        with pytest.raises(ReleaseFeedError, match="missing its bundle or manifest asset"):
            await fetch_latest(REPO, client=client)


async def test_fetch_latest_raises_when_manifest_asset_is_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_release_payload(
                assets=[{"name": "harmony-hub-x.tar.gz", "browser_download_url": "https://gh.example/t.tar.gz"}]
            ),
        )

    async with _client(handler) as client:
        with pytest.raises(ReleaseFeedError, match="missing its bundle or manifest asset"):
            await fetch_latest(REPO, client=client)


async def test_fetch_latest_raises_on_an_unparseable_manifest():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == LATEST_URL:
            return httpx.Response(
                200,
                json=_release_payload(
                    assets=[
                        {"name": "x.tar.gz", "browser_download_url": "https://gh.example/t.tar.gz"},
                        {"name": "x.manifest.json", "browser_download_url": "https://gh.example/m.json"},
                    ]
                ),
            )
        return httpx.Response(200, content=b"not json at all")

    async with _client(handler) as client:
        with pytest.raises(ReleaseFeedError, match="unreadable manifest"):
            await fetch_latest(REPO, client=client)


async def test_fetch_latest_wraps_a_network_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    async with _client(handler) as client:
        with pytest.raises(ReleaseFeedError, match="could not reach the release feed"):
            await fetch_latest(REPO, client=client)


async def test_download_writes_the_file_and_returns_its_size(tmp_path):
    content = b"a bundle's worth of bytes"
    expected_sha = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    dest = tmp_path / "bundle.tar.gz"
    async with _client(handler) as client:
        size = await download(
            "https://gh.example/bundle.tar.gz", dest, expected_sha256=expected_sha, max_bytes=1024, client=client
        )

    assert size == len(content)
    assert dest.read_bytes() == content


async def test_download_wraps_an_http_error_as_a_release_feed_error(tmp_path):
    """Regression test: a bare `httpx.HTTPStatusError` used to escape this

    function uncaught -- an asset a check found a moment ago, now gone (a
    release deleted mid-install, or a bad tar_url), surfaced two layers away
    as an unhandled exception instead of a message anyone could act on.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"Not Found")

    dest = tmp_path / "bundle.tar.gz"
    async with _client(handler) as client:
        with pytest.raises(ReleaseFeedError, match="could not download"):
            await download(
                "https://gh.example/bundle.tar.gz", dest, expected_sha256="0" * 64, max_bytes=1024, client=client
            )

    assert not dest.exists()


async def test_download_rejects_a_hash_mismatch_and_removes_the_partial_file(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"whatever bytes")

    dest = tmp_path / "bundle.tar.gz"
    async with _client(handler) as client:
        with pytest.raises(ReleaseFeedError, match="does not match the manifest's content hash"):
            await download(
                "https://gh.example/bundle.tar.gz", dest, expected_sha256="0" * 64, max_bytes=1024, client=client
            )

    assert not dest.exists()


async def test_download_refuses_a_download_over_the_byte_limit_and_removes_the_partial_file(tmp_path):
    content = b"x" * 2048

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    dest = tmp_path / "bundle.tar.gz"
    async with _client(handler) as client:
        with pytest.raises(ReleaseFeedError, match="exceeds the .*-byte limit"):
            await download(
                "https://gh.example/bundle.tar.gz",
                dest,
                expected_sha256=hashlib.sha256(content).hexdigest(),
                max_bytes=100,
                client=client,
            )

    assert not dest.exists()


def test_is_newer_treats_no_current_build_as_always_older():
    assert is_newer("20260101T000000-abc1234", None) is True


def test_is_newer_compares_build_ids_lexicographically():
    assert is_newer("20260830T120000-abc1234", "20260101T000000-def5678") is True
    assert is_newer("20260101T000000-abc1234", "20260830T120000-def5678") is False
    assert is_newer("20260101T000000-abc1234", "20260101T000000-abc1234") is False
