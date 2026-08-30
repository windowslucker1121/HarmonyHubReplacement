"""The update-check cache: check_now's caching/announce-once behaviour, and poll_forever's loop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from harmony_hub.events import EventBroker
from harmony_hub.update import check as check_module
from harmony_hub.update import state as state_module
from harmony_hub.update.check import CheckState, check_now, is_check_due, load, poll_forever, save, seconds_since
from harmony_hub.update.manifest import Manifest

REPO = "windowslucker1121/HarmonyHubReplacement"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def _manifest_json(build_id: str) -> bytes:
    manifest = Manifest(
        build_id=build_id,
        created_at="2026-08-30T12:00:00+00:00",
        file_count=1,
        byte_count=1,
        content_sha256="a" * 64,
    )
    return manifest.model_dump_json().encode("utf-8")


def _release_handler(build_id: str, *, tag: str = "v1.2.3"):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LATEST_URL:
            return httpx.Response(
                200,
                json={
                    "tag_name": tag,
                    "published_at": "2026-08-30T12:05:00Z",
                    "body": "",
                    "assets": [
                        {"name": "x.tar.gz", "browser_download_url": "https://gh.example/t.tar.gz", "size": 10},
                        {"name": "x.manifest.json", "browser_download_url": "https://gh.example/m.json"},
                    ],
                },
            )
        if str(request.url) == "https://gh.example/m.json":
            return httpx.Response(200, content=_manifest_json(build_id))
        raise AssertionError(f"unexpected request to {request.url}")

    return handler


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_load_missing_file_returns_a_fresh_state(tmp_path):
    state = load(tmp_path / "nope.json")
    assert state == CheckState()


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "update_check.json"
    original = CheckState(last_checked_at="2026-08-30T12:00:00+00:00", announced_build_id="build-1")
    save(original, path)
    assert load(path) == original


def test_load_a_corrupt_file_starts_fresh_rather_than_raising(tmp_path):
    path = tmp_path / "update_check.json"
    path.write_text("not json", encoding="utf-8")
    assert load(path) == CheckState()


def test_seconds_since_none_is_infinite():
    assert seconds_since(None) == float("inf")


def test_is_check_due_respects_the_floor(tmp_path):
    state = CheckState(last_checked_at=check_module._now())
    assert is_check_due(state, 3600) is False
    assert is_check_due(state, 0) is True


async def test_check_now_records_an_available_release_newer_than_current(tmp_path):
    state_module.save(state_module.UpdateState(current="20260101T000000-old0001"), tmp_path / "data" / "update_state.json")
    broker = EventBroker()

    async with _client(_release_handler("20260830T120000-new0001")) as client:
        result = await check_now(tmp_path, REPO, broker=broker, client=client)

    assert result.available is not None
    assert result.available.build_id == "20260830T120000-new0001"
    assert result.last_error is None
    assert result.announced_build_id == "20260830T120000-new0001"
    on_disk = load(check_module.state_path(tmp_path))
    assert on_disk.available.build_id == "20260830T120000-new0001"


async def test_check_now_reports_nothing_when_the_release_is_not_newer(tmp_path):
    state_module.save(
        state_module.UpdateState(current="20260830T120000-cur0001"), tmp_path / "data" / "update_state.json"
    )

    async with _client(_release_handler("20260101T000000-old0001")) as client:
        result = await check_now(tmp_path, REPO, client=client)

    assert result.available is None
    assert result.last_error is None


async def test_check_now_publishes_the_update_event_only_once_per_build(tmp_path):
    broker = EventBroker()

    async with _client(_release_handler("20260830T120000-new0001")) as client:
        await check_now(tmp_path, REPO, broker=broker, client=client)
        await check_now(tmp_path, REPO, broker=broker, client=client)

    update_events = [e for e in broker.history if e.type == "update"]
    assert len(update_events) == 1


async def test_check_now_on_failure_keeps_the_previously_known_release_and_records_the_error(tmp_path):
    async with _client(_release_handler("20260830T120000-new0001")) as client:
        first = await check_now(tmp_path, REPO, client=client)
    assert first.available is not None

    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    async with _client(failing_handler) as client:
        second = await check_now(tmp_path, REPO, client=client)

    assert second.available is not None
    assert second.available.build_id == first.available.build_id
    assert second.last_error is not None


async def test_poll_forever_checks_when_enabled_and_due(tmp_path):
    settings = SimpleNamespace(github_updates_enabled=True, github_repo=REPO, update_check_interval_hours=0.0001)

    async with _client(_release_handler("20260830T120000-new0001")) as client:
        task = asyncio.create_task(
            poll_forever(tmp_path, lambda: settings, client=client, wake_seconds=0.01)
        )
        for _ in range(200):
            if load(check_module.state_path(tmp_path)).last_checked_at is not None:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert load(check_module.state_path(tmp_path)).last_checked_at is not None


async def test_poll_forever_never_checks_when_disabled(tmp_path):
    settings = SimpleNamespace(github_updates_enabled=False, github_repo=REPO, update_check_interval_hours=0.0001)

    async def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call GitHub while updates are disabled")

    async with _client(boom) as client:
        task = asyncio.create_task(poll_forever(tmp_path, lambda: settings, client=client, wake_seconds=0.01))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert load(check_module.state_path(tmp_path)).last_checked_at is None
