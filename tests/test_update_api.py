"""The /api/update surface, driven through TestClient with real HMAC signatures.

Verify/stage/install/rollback themselves are covered without a TestClient in
tests/test_update_installer.py and tests/test_update_state.py; this file's
job is the plumbing around them -- auth, the deployed/not-deployed switch,
and the active-scene guard.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from harmony_hub.api import create_app
from harmony_hub.service import HubSettings
from harmony_hub.update import auth as update_auth
from harmony_hub.update import state as state_module
from harmony_hub.update.bundle import build_bundle

CONFIG_WITH_SCENE = {
    "version": 1,
    "devices": [{"id": "tv", "name": "TV", "backend": "virtual", "config": {"commands": ["on", "off"]}}],
    "scenes": [{"id": "watch_tv", "name": "Watch TV", "devices": ["tv"]}],
}


def _seed_working_repo(root, build_id):
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\ndependencies = []\n", encoding="utf-8")
    src_hub = root / "src" / "harmony_hub"
    src_hub.mkdir(parents=True)
    (src_hub / "__init__.py").write_text("", encoding="utf-8")
    (src_hub / "server.py").write_text(f"BUILD = {build_id!r}\ndef main(): return 0\n", encoding="utf-8")
    src_receiver = root / "src" / "harmony_receiver"
    src_receiver.mkdir(parents=True)
    (src_receiver / "__init__.py").write_text("", encoding="utf-8")
    (src_receiver / "receiver.py").write_text("def run(): return 0\n", encoding="utf-8")
    return root


def _make_bundle(tmp_path, build_id):
    repo = _seed_working_repo(tmp_path / f"repo-{build_id}", build_id)
    tar_path = tmp_path / f"{build_id}.tar.gz"
    manifest = build_bundle(repo, tar_path, build_id=build_id)
    return manifest, tar_path


def _sign(update_root, manifest, nonce):
    token = update_auth.load_or_create_token(update_root / "data" / "update_token")
    signature = update_auth.sign(token, nonce, manifest.content_sha256)
    return {"X-Harmony-Nonce": str(nonce), "X-Harmony-Signature": signature}


def _push(client, tar_path, manifest, headers, **params):
    with tar_path.open("rb") as f:
        return client.post(
            "/api/update",
            params=params,
            data={"manifest": manifest.model_dump_json()},
            files={"bundle": (tar_path.name, f, "application/gzip")},
            headers=headers,
        )


@pytest.fixture
def update_root(tmp_path):
    root = tmp_path / "device_root"
    root.mkdir()
    return root


def _make_client(tmp_path, update_root=None, config=None, updates_enabled=True):
    config_path = tmp_path / "hub_config.json"
    buttons_path = tmp_path / "buttons.json"
    config_path.write_text(json.dumps(config or {"version": 1, "devices": [], "scenes": []}), encoding="utf-8")
    buttons_path.write_text("{}", encoding="utf-8")

    settings = HubSettings(
        config_path=config_path, buttons_path=buttons_path, source="none", updates_enabled=updates_enabled
    )
    app = create_app(settings, settings_path=tmp_path / "hub_settings.json", update_root=update_root)
    return TestClient(app)


def test_version_reports_not_deployed_when_there_is_no_release_system(tmp_path):
    with _make_client(tmp_path, update_root=None) as client:
        response = client.get("/api/version")
        assert response.status_code == 200
        body = response.json()
        assert body["deployed"] is False
        assert body["build_id"] is None


def test_update_routes_404_when_there_is_no_release_system(tmp_path):
    with _make_client(tmp_path, update_root=None) as client:
        assert client.post("/api/update/rollback").status_code == 404
        assert client.get("/api/update/status").status_code == 404
        assert client.get("/api/update/history").status_code == 404


def test_version_reports_deployed_with_nothing_installed_yet(tmp_path, update_root):
    with _make_client(tmp_path, update_root=update_root) as client:
        body = client.get("/api/version").json()
        assert body["deployed"] is True
        assert body["build_id"] is None


def test_the_first_version_check_ever_generates_the_update_token(tmp_path, update_root):
    """No push has to happen first -- an SSH-based setup fetches the token right after the first boot."""
    with _make_client(tmp_path, update_root=update_root) as client:
        body = client.get("/api/version").json()
        assert body["token_fingerprint"] is not None
        assert (update_root / "data" / "update_token").is_file()


def test_push_without_signature_headers_is_401(tmp_path, update_root):
    manifest, tar_path = _make_bundle(tmp_path, "build-1")
    with _make_client(tmp_path, update_root=update_root) as client:
        response = _push(client, tar_path, manifest, headers={})
        assert response.status_code == 401


def test_push_with_the_wrong_signature_is_401(tmp_path, update_root):
    manifest, tar_path = _make_bundle(tmp_path, "build-1")
    with _make_client(tmp_path, update_root=update_root) as client:
        headers = {"X-Harmony-Nonce": "1", "X-Harmony-Signature": "0" * 64}
        response = _push(client, tar_path, manifest, headers=headers)
        assert response.status_code == 401


def test_push_with_mismatched_content_hash_is_rejected(tmp_path, update_root):
    manifest, tar_path = _make_bundle(tmp_path, "build-1")
    tampered = manifest.model_copy(update={"content_sha256": "a" * 64})
    headers = _sign(update_root, tampered, nonce=1)
    with _make_client(tmp_path, update_root=update_root) as client:
        response = _push(client, tar_path, tampered, headers=headers)
        assert response.status_code == 400
        assert "content hash" in response.text


def test_a_valid_push_installs_and_is_reflected_in_version(tmp_path, update_root):
    manifest, tar_path = _make_bundle(tmp_path, "build-1")
    headers = _sign(update_root, manifest, nonce=1)
    with _make_client(tmp_path, update_root=update_root) as client:
        response = _push(client, tar_path, manifest, headers=headers)
        assert response.status_code == 202
        assert response.json() == {"build_id": "build-1", "restarting": True}

        version = client.get("/api/version").json()
        assert version["build_id"] == "build-1"
        assert version["trial"]["release"] == "build-1"
        assert version["token_fingerprint"] is not None

        assert client.get("/api/update/history").json() == []


def test_a_replayed_nonce_is_rejected_even_after_a_successful_push(tmp_path, update_root):
    manifest, tar_path = _make_bundle(tmp_path, "build-1")
    headers = _sign(update_root, manifest, nonce=7)
    with _make_client(tmp_path, update_root=update_root) as client:
        assert _push(client, tar_path, manifest, headers=headers).status_code == 202

        manifest_2, tar_path_2 = _make_bundle(tmp_path, "build-2")
        replay_headers = _sign(update_root, manifest_2, nonce=7)
        response = _push(client, tar_path_2, manifest_2, headers=replay_headers)
        assert response.status_code == 401


def test_pushing_a_build_id_already_installed_is_rejected(tmp_path, update_root):
    manifest, tar_path = _make_bundle(tmp_path, "build-1")
    with _make_client(tmp_path, update_root=update_root) as client:
        headers = _sign(update_root, manifest, nonce=1)
        assert _push(client, tar_path, manifest, headers=headers).status_code == 202

        headers_2 = _sign(update_root, manifest, nonce=2)
        response = _push(client, tar_path, manifest, headers=headers_2)
        assert response.status_code == 422
        assert "already installed" in response.text


def test_updates_disabled_in_settings_refuses_the_push(tmp_path, update_root):
    manifest, tar_path = _make_bundle(tmp_path, "build-1")
    headers = _sign(update_root, manifest, nonce=1)
    with _make_client(tmp_path, update_root=update_root, updates_enabled=False) as client:
        response = _push(client, tar_path, manifest, headers=headers)
        assert response.status_code == 403


def test_pushing_while_a_scene_is_active_needs_force(tmp_path, update_root):
    manifest, tar_path = _make_bundle(tmp_path, "build-1")
    with _make_client(tmp_path, update_root=update_root, config=CONFIG_WITH_SCENE) as client:
        assert client.post("/api/scenes/watch_tv/activate").status_code == 200

        headers = _sign(update_root, manifest, nonce=1)
        blocked = _push(client, tar_path, manifest, headers=headers)
        assert blocked.status_code == 409

        headers_2 = _sign(update_root, manifest, nonce=2)
        forced = _push(client, tar_path, manifest, headers=headers_2, force="true")
        assert forced.status_code == 202


def test_rollback_with_nothing_to_roll_back_to_is_409(tmp_path, update_root):
    with _make_client(tmp_path, update_root=update_root) as client:
        response = client.post("/api/update/rollback")
        assert response.status_code == 409


def test_rollback_after_a_successful_push_restores_the_prior_release(tmp_path, update_root):
    manifest_1, tar_1 = _make_bundle(tmp_path, "build-1")
    manifest_2, tar_2 = _make_bundle(tmp_path, "build-2")
    with _make_client(tmp_path, update_root=update_root) as client:
        assert _push(client, tar_1, manifest_1, headers=_sign(update_root, manifest_1, 1)).status_code == 202
        assert _push(client, tar_2, manifest_2, headers=_sign(update_root, manifest_2, 2)).status_code == 202
        assert client.get("/api/version").json()["build_id"] == "build-2"

        response = client.post("/api/update/rollback")
        assert response.status_code == 202
        assert response.json()["build_id"] == "build-1"
        assert client.get("/api/version").json()["build_id"] == "build-1"

        history = client.get("/api/update/history").json()
        last = history[-1]
        assert last["build_id"] == "build-2"
        assert last["outcome"] == "rolled_back"


def test_update_status_surfaces_recent_progress_events(tmp_path, update_root):
    manifest, tar_path = _make_bundle(tmp_path, "build-1")
    with _make_client(tmp_path, update_root=update_root) as client:
        _push(client, tar_path, manifest, headers=_sign(update_root, manifest, 1))
        status = client.get("/api/update/status").json()
        assert status["busy"] is False
        assert any("build-1" in event["detail"] for event in status["recent"])
