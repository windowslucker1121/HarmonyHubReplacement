"""HTTP and WebSocket API for the hub.

This is the contract the UI is written against, and the reason the front end
can be swapped or ported without touching anything else. Kept deliberately
thin: every route translates a request into one call on `HubRuntime` or
`SceneEngine`.

The web layer starts once and never restarts. Underneath it `HubRuntime`
supervises the hub itself, which can be stopped, reconfigured and restarted
while this stays up -- so a missing radio or an unreadable configuration file
is something the settings page reports rather than something that stops the
settings page existing. Routes that genuinely need a live engine answer 409;
everything to do with *configuring* the hub keeps working while it is down,
because being down is when you most need to configure it.

Three endpoints exist purely to make the editor buildable without hard-coding
knowledge of each backend -- `/api/backends` returns each backend's config
schema so the device form can be generated, `/api/devices/{id}/commands`
returns what a device can actually do so binding a button is a dropdown
rather than a free-text field, and `/api/devices/{id}/entities` lists what a
device *could* offer commands for, for the one backend whose vocabulary is
not fixed but chosen from a live instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import backends
from . import settings as settings_module
from .diagnostics import Check, run_checks, try_settings
from .discovery import (
    DEFAULT_SNIFF_TIMEOUT,
    DEFAULT_SNIFF_VERIFY_TIMEOUT,
    DEFAULT_TIMEOUT,
    DiscoveryMethod,
    DiscoveryStatus,
)
from .engine import SceneEngine
from .events import HubEvent
from .models import HubConfig
from .runtime import ConfigUnreadable, HubRuntime, RuntimeStatus
from .settings import HubSettings
from .update import auth as update_auth
from .update import confirm as update_confirm
from .update import installer as update_installer
from .update import manifest as update_manifest
from .update import state as update_state_module

logger = logging.getLogger("HUB.api")

#: What a fresh page load reads before deciding what else to fetch. A
#: browser that heuristically caches these can pin itself to the UI from
#: before a remote update -- see `no_cache_for_ui_entry_points` below.
NO_CACHE_UI_PATHS = {"/", "/index.html", "/flutter_bootstrap.js", "/flutter_service_worker.js", "/main.dart.js"}


class ButtonInfo(BaseModel):
    key: str
    label: str
    signatures: List[str]


class LearnedButton(BaseModel):
    """One button being named, and the raw signatures it was seen to send."""

    # Same shape as scene and device ids: these become binding keys, and a
    # button called "Volume Up " would be a binding nothing could ever match.
    key: str = Field(pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1)
    signatures: List[str] = Field(min_length=1)


class LearnRequest(BaseModel):
    buttons: List[LearnedButton] = Field(min_length=1)


class DeviceStatus(BaseModel):
    id: str
    name: str
    backend: str
    running: bool
    ok: bool
    detail: str = ""


class SceneSummary(BaseModel):
    id: str
    name: str
    icon: Optional[str] = None
    devices: List[str] = []
    bound_buttons: int = 0


class FocusInfo(BaseModel):
    """What the SmartHome +/- keys currently follow."""

    device: str
    target: str
    label: str
    #: Whether the focused target can actually be stepped either way. A
    #: toggled switch takes the focus like anything else does, but +/- would
    #: only ever report "nothing to turn up" for it -- this is what lets the
    #: app show that up front rather than waiting for a press to find out.
    can_adjust: bool


class HubState(BaseModel):
    active_scene: Optional[str]
    scenes: List[SceneSummary]
    devices: List[DeviceStatus]
    button_count: int
    hub: RuntimeStatus
    #: `None` before anything has claimed the SmartHome keys, or once the
    #: hub is stopped -- the focus lives on the engine, and there is none.
    focus: Optional[FocusInfo] = None
    #: Whether button presses are logged but not acted on. Lives on the
    #: engine, like `active_scene` and `focus` -- always `False` while the
    #: hub is stopped, for the same reason those are `None` then.
    paused: bool = False


class BackendInfo(BaseModel):
    name: str
    label: str
    description: str
    config_schema: Dict[str, Any]
    # Whether this backend needs the pairing routes below before it will work.
    pairable: bool = False
    # The words the pairing screen should use. The mechanism generalises but
    # the wording does not -- a television shows a six-digit code, a Home
    # Assistant issues a long token from a web page -- so the backend supplies
    # its own rather than the app hard-coding either.
    pair_label: str = ""
    pair_hint: str = ""
    pair_input_label: str = ""
    pair_input_multiline: bool = False
    # Whether `/api/backends/{name}/discover` exists for this backend, and
    # which config field its results fill in. Sent from the hub for the same
    # reason `pairable` is: the app should not keep its own list of which
    # backend names happen to support discovery.
    discoverable: bool = False
    discover_field: str = ""
    # Whether this backend needs the learn routes below -- for the same
    # reason `pairable` does: the app should not keep its own list of which
    # backend names happen to support learning commands from a remote.
    learnable: bool = False
    learn_label: str = ""
    learn_hint: str = ""
    #: Whether a captured command can be replayed back through the
    #: transmitter to check it before saving -- false for a receive-only
    #: install, where there is nothing to play it back through.
    learn_verifiable: bool = False


class EntityInfo(BaseModel):
    """One thing a device can offer a command for, before anyone has picked it."""

    entity_id: str
    name: str
    domain: str
    state: str = ""
    controllable: bool = True


class CommandInfo(BaseModel):
    name: str
    label: str
    description: str = ""
    params: Dict[str, Any] = {}
    repeatable: bool = False


class PairFinishRequest(BaseModel):
    code: str


class SimulateRequest(BaseModel):
    kind: str = "press"


class TestCommandRequest(BaseModel):
    command: str
    params: Dict[str, Any] = {}


class LearnStatusInfo(BaseModel):
    """Where one IR learn job has got to. Shaped like `ir.learn.LearnStatus`,
    kept as its own model for the same reason `CommandInfo` exists alongside
    `backends.Command`: the HTTP contract stays decoupled from the backend
    interface's own return types."""

    state: str
    detail: str = ""
    decoded: str = ""
    pulses: int = 0


class LearnSaveRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1)
    repeatable: bool = False
    repeats: int = Field(default=1, ge=1, le=20)


class TrialInfo(BaseModel):
    release: str
    attempts: int
    started_at: str
    from_release: Optional[str] = None


class VersionInfo(BaseModel):
    """What this hub is running, and whether it can be pushed to at all."""

    deployed: bool
    updates_enabled: bool
    build_id: Optional[str] = None
    git_sha: str = ""
    git_dirty: bool = False
    built_at: Optional[str] = None
    web_build_id: Optional[str] = None
    previous: Optional[str] = None
    trial: Optional[TrialInfo] = None
    token_fingerprint: Optional[str] = None


class UpdateResult(BaseModel):
    build_id: Optional[str]
    restarting: bool


class UpdateStatusInfo(BaseModel):
    busy: bool
    recent: List[HubEvent] = []


class UpdateHistoryEntry(BaseModel):
    build_id: str
    installed_at: str
    outcome: str


def create_app(
    settings: Optional[HubSettings] = None,
    static_dir: Optional[Path] = None,
    settings_path: Optional[str | Path] = None,
    settings_error: Optional[str] = None,
    update_root: Optional[str | Path] = None,
) -> FastAPI:
    """Builds the API. `settings` is injectable so tests can drive a hub with no radio.

    `update_root` turns on `/api/update` and friends -- the release layout
    (`releases/`, `data/update_state.json`, `bin/harmony-launch`) that
    `harmony_launch.main` sets `HARMONY_UPDATE_ROOT` to before exec'ing this
    process. Left `None` for an ordinary `harmony-hub` run (a dev checkout,
    or any install that predates this feature): the update routes then
    answer 404 rather than pretending to work with nowhere to stage a
    release.
    """
    runtime = HubRuntime(
        settings or HubSettings(),
        settings_path=settings_path or settings_module.DEFAULT_PATH,
        settings_error=settings_error,
    )
    update_root = Path(update_root) if update_root else None
    update_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Deliberately not guarded: `HubRuntime.start` does not raise. A hub
        # that cannot come up leaves this app serving anyway, which is the
        # whole point -- the page that explains the failure must outlive it.
        if runtime.settings.autostart:
            await runtime.start()
        else:
            logger.info("Autostart is off; the hub is waiting to be started from Settings")

        # Reaching this point *is* the health signal a trial release needs
        # to confirm: the ASGI lifespan has completed startup without
        # raising, which `HubRuntime.start()`'s own contract guarantees
        # regardless of whether the hub itself came up -- a `failed` runtime
        # state must never look like a bad deploy. See `update/confirm.py`.
        confirm_task = None
        if update_root is not None:
            confirm_task = asyncio.create_task(update_confirm.schedule_confirmation(update_root))

        try:
            yield
        finally:
            if confirm_task is not None:
                confirm_task.cancel()
                with contextlib.suppress(BaseException):
                    await confirm_task
            await runtime.stop()

    app = FastAPI(title="Harmony Hub", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    # The Flutter app runs on its own dev server during development, and on
    # a phone it is not same-origin at all. This API is LAN-local and holds
    # no credentials of its own, so permissive CORS costs nothing here --
    # revisit if authentication is ever added.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def no_cache_for_ui_entry_points(request: Request, call_next):
        """Stops a browser from pinning itself to yesterday's UI after a remote update.

        Only the entry points a fresh page load actually reads before
        deciding what else to fetch -- everything under `/assets/` etc. can
        keep the browser's normal heuristics, since a stale cache there just
        means an extra round trip, not a UI stuck on an old build.
        """
        response = await call_next(request)
        if request.url.path in NO_CACHE_UI_PATHS:
            response.headers["Cache-Control"] = "no-cache"
        return response

    def _engine() -> SceneEngine:
        """The live engine, or a 409 saying where to go and turn it on."""
        if runtime.service is None:
            raise HTTPException(409, f"the hub is not running ({runtime.detail}) -- start it from Settings")
        return runtime.service.engine

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    async def _device_statuses() -> List[DeviceStatus]:
        statuses = []
        for device in runtime.config.devices:
            backend = runtime.service.engine.backend_for(device.id) if runtime.service else None
            if backend is None:
                statuses.append(
                    DeviceStatus(
                        id=device.id, name=device.name, backend=device.backend,
                        running=False, ok=False,
                        detail="not started" if runtime.service else "the hub is stopped",
                    )
                )
                continue
            try:
                health = await backend.health()
            except Exception as err:
                health = backends.Health(ok=False, detail=str(err))
            statuses.append(
                DeviceStatus(
                    id=device.id, name=device.name, backend=device.backend,
                    running=True, ok=health.ok, detail=health.detail,
                )
            )
        return statuses

    def _focus_info() -> Optional[FocusInfo]:
        focus = runtime.service.engine.focus if runtime.service else None
        if focus is None:
            return None
        backend = runtime.service.engine.backend_for(focus.device)
        can_adjust = backend is not None and (
            backend.adjust_command(focus.target, "up") is not None
            or backend.adjust_command(focus.target, "down") is not None
        )
        return FocusInfo(device=focus.device, target=focus.target, label=focus.label, can_adjust=can_adjust)

    @app.get("/api/state", response_model=HubState)
    async def get_state() -> HubState:
        """Answers whether or not the hub is running, so the app always has something to draw."""
        return HubState(
            active_scene=runtime.service.engine.active_scene if runtime.service else None,
            scenes=[
                SceneSummary(
                    id=s.id, name=s.name, icon=s.icon, devices=s.devices,
                    bound_buttons=len(s.bindings),
                )
                for s in runtime.config.scenes
            ],
            devices=await _device_statuses(),
            button_count=len(runtime.buttons),
            hub=runtime.status(),
            focus=_focus_info(),
            paused=runtime.service.engine.paused if runtime.service else False,
        )

    def _button_list(buttons) -> List[ButtonInfo]:
        return [
            ButtonInfo(key=p.key, label=p.label, signatures=sorted(p.signatures))
            for p in buttons
        ]

    @app.get("/api/buttons", response_model=List[ButtonInfo])
    async def get_buttons() -> List[ButtonInfo]:
        return _button_list(runtime.reload_buttons())

    @app.post("/api/buttons/learn", response_model=List[ButtonInfo])
    async def learn_buttons(request: LearnRequest) -> List[ButtonInfo]:
        """Names signatures the remote has been seen to send.

        The signatures come from the live event stream: an unlearned button
        is published under its own hex, which is what makes it visible enough
        to be named in the first place.
        """
        try:
            buttons = runtime.learn_buttons(
                [(b.key, b.label, b.signatures) for b in request.buttons]
            )
        except OSError as err:
            raise HTTPException(409, f"could not write {runtime.settings.buttons_path}: {err}")
        return _button_list(buttons)

    @app.delete("/api/buttons/{key}", response_model=List[ButtonInfo])
    async def forget_button(key: str) -> List[ButtonInfo]:
        if key not in runtime.buttons:
            raise HTTPException(404, f"no button '{key}'")
        try:
            buttons = runtime.forget_button(key)
        except OSError as err:
            raise HTTPException(409, f"could not write {runtime.settings.buttons_path}: {err}")
        return _button_list(buttons)

    # ------------------------------------------------------------------
    # Configuration
    #
    # Works with the hub stopped, on purpose: editing scenes should not
    # require the equipment to be reachable, and a hub that will not start is
    # exactly when its configuration needs changing.
    # ------------------------------------------------------------------

    @app.get("/api/config", response_model=HubConfig)
    async def get_config() -> HubConfig:
        return runtime.config

    @app.put("/api/config", response_model=HubConfig)
    async def put_config(new_config: HubConfig, force: bool = False) -> HubConfig:
        # Validation already happened in the model, so a bad edit is a 422
        # from FastAPI before anything is written -- the previous config
        # stays live rather than being replaced with something broken.
        try:
            await runtime.save_config(new_config, force=force)
        except ConfigUnreadable as err:
            # The in-memory config is an empty stand-in for a file that would
            # not parse. Saving it would delete every scene in that file, so
            # replacing it has to be asked for explicitly.
            raise HTTPException(409, f"{err} -- fix the file, or save again with ?force=true to replace it")
        return runtime.config

    @app.get("/api/backends", response_model=List[BackendInfo])
    async def get_backends() -> List[BackendInfo]:
        infos = []
        for cls in sorted(backends.available().values(), key=lambda c: c.name):
            pairable = issubclass(cls, backends.Pairable)
            learnable = issubclass(cls, backends.Learnable)
            infos.append(
                BackendInfo(
                    name=cls.name,
                    label=cls.label or cls.name,
                    description=cls.description,
                    config_schema=cls.config_schema(),
                    pairable=pairable,
                    pair_label=cls.pair_label if pairable else "",
                    pair_hint=cls.pair_hint if pairable else "",
                    pair_input_label=cls.pair_input_label if pairable else "",
                    pair_input_multiline=cls.pair_input_multiline if pairable else False,
                    discoverable=bool(cls.discover_field),
                    discover_field=cls.discover_field,
                    learnable=learnable,
                    learn_label=cls.learn_label if learnable else "",
                    learn_hint=cls.learn_hint if learnable else "",
                    learn_verifiable=cls.learn_verifiable if learnable else False,
                )
            )
        return infos

    @app.get("/api/backends/androidtv/discover")
    async def discover_android_tv(timeout: float = 3.0) -> List[Dict[str, Any]]:
        """Android TV devices announcing themselves on the network.

        Saves hunting for the box's IP address in a router admin page. An
        empty list means mDNS found nothing, not that anything went wrong.
        """
        from .backends.androidtv import discover

        return await discover(timeout=min(max(timeout, 0.5), 10.0))

    @app.get("/api/backends/homeassistant/discover")
    async def discover_home_assistant(timeout: float = 3.0) -> List[Dict[str, Any]]:
        """Home Assistant instances announcing themselves on the network.

        Same job and same failure mode as the route above: nothing found is an
        empty list, because the address field is still there to type into.
        """
        from .backends.homeassistant import discover

        return await discover(timeout=min(max(timeout, 0.5), 10.0))

    @app.get("/api/backends/denon/discover")
    async def discover_denon(timeout: float = 3.0) -> List[Dict[str, Any]]:
        """Denon and Marantz receivers announcing themselves over SSDP.

        Same job and failure mode as the two routes above, over a different
        protocol: Denon does not announce over mDNS. A receiver in standby
        with Network Control off is not on the network at all, so it will
        not appear here either.
        """
        from .backends.denon import discover

        return await discover(timeout=min(max(timeout, 0.5), 10.0))

    @app.get("/api/backends/lgtv/discover")
    async def discover_lg_tv(timeout: float = 3.0) -> List[Dict[str, Any]]:
        """LG webOS TVs announcing themselves over SSDP.

        Same job and failure mode as the Denon route above -- webOS does not
        announce over mDNS either.
        """
        from .backends.lgtv import discover

        return await discover(timeout=min(max(timeout, 0.5), 10.0))

    # ------------------------------------------------------------------
    # Settings and the hub's own lifecycle
    # ------------------------------------------------------------------

    @app.get("/api/settings", response_model=HubSettings)
    async def get_settings() -> HubSettings:
        return runtime.settings

    @app.put("/api/settings", response_model=RuntimeStatus)
    async def put_settings(new_settings: HubSettings, restart: bool = False) -> RuntimeStatus:
        """Saves settings, optionally restarting the hub onto them.

        Bind host and port are saved but not applied -- moving the live
        listener would take this page's URL with it. The reply's
        `pending_restart` says when that has happened.
        """
        await runtime.apply_settings(new_settings, restart=restart)
        return runtime.status()

    @app.get("/api/hub", response_model=RuntimeStatus)
    async def get_hub() -> RuntimeStatus:
        return runtime.status()

    @app.post("/api/hub/start", response_model=RuntimeStatus)
    async def start_hub() -> RuntimeStatus:
        await runtime.start()
        return runtime.status()

    @app.post("/api/hub/stop", response_model=RuntimeStatus)
    async def stop_hub() -> RuntimeStatus:
        await runtime.stop()
        return runtime.status()

    @app.post("/api/hub/restart", response_model=RuntimeStatus)
    async def restart_hub() -> RuntimeStatus:
        await runtime.restart()
        return runtime.status()

    @app.post("/api/hub/pause")
    async def pause_hub() -> Dict[str, Any]:
        """Stops commands from executing, without stopping the hub from hearing them.

        For trying a real remote against real hardware -- a Pi wired up for
        testing -- without any press reaching a device. Presses still show up
        live; `resume_hub` is the only way back.
        """
        engine = _engine()
        engine.paused = True
        runtime.broker.publish(HubEvent(type="status", ok=True, detail="Paused -- logging presses, not executing them"))
        return {"paused": True}

    @app.post("/api/hub/resume")
    async def resume_hub() -> Dict[str, Any]:
        engine = _engine()
        engine.paused = False
        runtime.broker.publish(HubEvent(type="status", ok=True, detail="Resumed -- commands execute normally again"))
        return {"paused": False}

    @app.get("/api/checks", response_model=List[Check])
    async def get_checks() -> List[Check]:
        """Everything worth verifying about this hub as it stands."""
        return await run_checks(runtime, static_dir)

    @app.post("/api/settings/try", response_model=List[Check])
    async def try_these_settings(candidate: HubSettings) -> List[Check]:
        """Whether settings would work, without saving them."""
        return await try_settings(runtime, candidate)

    # ------------------------------------------------------------------
    # Remote update
    #
    # Every route below is a thin translation onto `update.installer.install`
    # and `update.state`, neither of which import FastAPI -- so the actual
    # install logic is tested without a TestClient. This layer's own job is
    # narrow: is this hub even deployed under the release system, is the
    # request who it claims to be, and is there already an update running.
    # ------------------------------------------------------------------

    def _update_state_path() -> Path:
        return update_confirm.state_path(update_root)

    def _update_token_path() -> Path:
        return update_root / "data" / "update_token"

    def _require_deployed() -> None:
        if update_root is None:
            raise HTTPException(
                404,
                "this hub was not started under the release system -- see RASPBERRY_PI_DEPLOYMENT.md",
            )

    def _read_release_manifest(build_id: str) -> Optional[update_manifest.Manifest]:
        path = update_state_module.release_dir(update_root, build_id) / "manifest.json"
        if not path.is_file():
            return None
        try:
            return update_manifest.Manifest.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Release %s has no readable manifest.json", build_id)
            return None

    def _verify_update_request(
        headers, token: bytes, state: update_state_module.UpdateState, content_sha256: str
    ) -> int:
        nonce_header = headers.get("X-Harmony-Nonce")
        signature = headers.get("X-Harmony-Signature")
        if not nonce_header or not signature:
            raise HTTPException(401, "missing X-Harmony-Nonce/X-Harmony-Signature headers")
        try:
            nonce = int(nonce_header)
        except ValueError:
            raise HTTPException(401, "X-Harmony-Nonce must be an integer")
        try:
            update_auth.verify(token, nonce, content_sha256, signature, state.last_nonce)
        except update_auth.InvalidSignature as err:
            raise HTTPException(401, str(err)) from err
        return nonce

    async def _save_upload(upload: UploadFile, dest: Path) -> "tuple[int, str]":
        """Streams the upload to `dest`, refusing it mid-stream once it's clearly too big."""
        hasher = hashlib.sha256()
        size = 0
        with dest.open("wb") as f:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > update_installer.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"bundle exceeds the {update_installer.MAX_UPLOAD_BYTES}-byte limit")
                hasher.update(chunk)
                f.write(chunk)
        return size, hasher.hexdigest()

    def _request_restart() -> None:
        """Asks uvicorn to stop serving. `main()` re-execs into the launcher once it does."""
        app.state.restart_requested = True
        uvicorn_server = getattr(app.state, "uvicorn_server", None)
        if uvicorn_server is not None:
            uvicorn_server.should_exit = True

    @app.get("/api/version", response_model=VersionInfo)
    async def get_version() -> VersionInfo:
        """What this hub is running. Answers even while the hub itself is `failed` -- see lifespan above."""
        if update_root is None:
            return VersionInfo(deployed=False, updates_enabled=runtime.settings.updates_enabled)

        state = update_state_module.load(_update_state_path())
        manifest_obj = _read_release_manifest(state.current) if state.current else None
        # Generated here rather than waiting for the first push: `/api/version`
        # is the first thing anything -- `harmony-deploy setup`, the Settings
        # screen, a bare curl -- asks after a device comes up, so this is
        # already the earliest point a token could be fetched from over SSH
        # or shown as a fingerprint. Idempotent: reads the existing token
        # back unchanged once one exists.
        token_fp = update_auth.fingerprint(update_auth.load_or_create_token(_update_token_path()))

        return VersionInfo(
            deployed=True,
            updates_enabled=runtime.settings.updates_enabled,
            build_id=state.current,
            git_sha=manifest_obj.git_sha if manifest_obj else "",
            git_dirty=manifest_obj.git_dirty if manifest_obj else False,
            built_at=manifest_obj.created_at if manifest_obj else None,
            web_build_id=manifest_obj.web_build_id if manifest_obj else None,
            previous=state.previous,
            trial=TrialInfo(**state.trial.model_dump(by_alias=False)) if state.trial else None,
            token_fingerprint=token_fp,
        )

    @app.post("/api/update", response_model=UpdateResult, status_code=202)
    async def push_update(
        request: Request,
        manifest: str = Form(...),
        bundle: UploadFile = File(...),
        force: bool = False,
    ) -> UpdateResult:
        """Receives one bundle: verify, stage, install dependencies, smoke-test, activate, restart.

        Nothing about the running hub changes until the very last step --
        see `update.installer.install` for the ordering this relies on.
        """
        _require_deployed()
        if not runtime.settings.updates_enabled:
            raise HTTPException(403, "remote updates are disabled in Settings")
        if update_lock.locked():
            raise HTTPException(409, "an update is already in progress")

        try:
            manifest_obj = update_manifest.Manifest.model_validate_json(manifest)
        except Exception as err:
            raise HTTPException(400, f"invalid manifest: {err}") from err

        if not force and runtime.service is not None and runtime.service.engine.active_scene:
            raise HTTPException(409, "a scene is active -- retry with ?force=true, or wait until it's idle")

        async with update_lock:
            incoming_dir = update_root / "incoming"
            incoming_dir.mkdir(parents=True, exist_ok=True)
            tar_path = incoming_dir / f"{manifest_obj.build_id}.tar.gz"
            try:
                size, content_sha256 = await _save_upload(bundle, tar_path)
                if content_sha256 != manifest_obj.content_sha256:
                    raise HTTPException(400, "uploaded bytes do not match the manifest's content hash")

                token = update_auth.load_or_create_token(_update_token_path())
                current_state = update_state_module.load(_update_state_path())
                nonce = _verify_update_request(request.headers, token, current_state, content_sha256)
                # Recorded before installing anything: a signature must not
                # be replayable even if the install itself goes on to fail.
                update_state_module.save(
                    current_state.model_copy(update={"last_nonce": nonce}), _update_state_path()
                )

                try:
                    new_state = await update_installer.install(
                        update_root, tar_path, manifest_obj, broker=runtime.broker
                    )
                except update_installer.InstallError as err:
                    raise HTTPException(422, str(err)) from err
            finally:
                tar_path.unlink(missing_ok=True)

        _request_restart()
        return UpdateResult(build_id=new_state.current, restarting=True)

    @app.get("/api/update/status", response_model=UpdateStatusInfo)
    async def get_update_status() -> UpdateStatusInfo:
        _require_deployed()
        recent = [event for event in runtime.broker.history if event.type == "update"][-20:]
        return UpdateStatusInfo(busy=update_lock.locked(), recent=recent)

    @app.get("/api/update/history", response_model=List[UpdateHistoryEntry])
    async def get_update_history() -> List[UpdateHistoryEntry]:
        _require_deployed()
        state = update_state_module.load(_update_state_path())
        return [UpdateHistoryEntry(**entry.model_dump()) for entry in state.history]

    @app.post("/api/update/rollback", response_model=UpdateResult, status_code=202)
    async def rollback_update() -> UpdateResult:
        """Activates the previous release and restarts onto it. Needs no signature.

        Rollback only ever moves between releases already on this device --
        the same risk class as `/api/hub/restart`, which needs none either.
        Requiring a secret to undo a bad deploy would be exactly backwards.
        """
        _require_deployed()
        if update_lock.locked():
            raise HTTPException(409, "an update is already in progress")

        state = update_state_module.load(_update_state_path())
        try:
            new_state = update_state_module.rollback(state)
        except update_state_module.NoPreviousRelease as err:
            raise HTTPException(409, str(err)) from err

        update_state_module.save(new_state, _update_state_path())
        runtime.broker.publish(HubEvent(type="update", ok=True, detail=f"Rolling back to {new_state.current}"))
        _request_restart()
        return UpdateResult(build_id=new_state.current, restarting=True)

    # ------------------------------------------------------------------
    # Finding the remote's address
    # ------------------------------------------------------------------

    @app.post("/api/radio/discover", response_model=DiscoveryStatus)
    async def start_discovery(
        method: DiscoveryMethod = "hub",
        timeout: Optional[float] = None,
        verify_timeout: Optional[float] = None,
    ) -> DiscoveryStatus:
        # No hub-agnostic single default: the hub handshake is quick once a
        # Hub answers, while the sniff needs long enough to actually catch
        # the remote transmitting on its own, so each method gets its own.
        default_timeout = DEFAULT_TIMEOUT if method == "hub" else DEFAULT_SNIFF_TIMEOUT
        resolved_timeout = min(max(timeout if timeout is not None else default_timeout, 5.0), 300.0)
        resolved_verify = min(
            max(verify_timeout if verify_timeout is not None else DEFAULT_SNIFF_VERIFY_TIMEOUT, 5.0), 120.0
        )
        return await runtime.start_discovery(resolved_timeout, method=method, verify_timeout=resolved_verify)

    @app.get("/api/radio/discover", response_model=DiscoveryStatus)
    async def discovery_status() -> DiscoveryStatus:
        return runtime.discovery.status

    @app.post("/api/radio/discover/cancel", response_model=DiscoveryStatus)
    async def cancel_discovery() -> DiscoveryStatus:
        return runtime.discovery.cancel()

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    def _running(device_id: str) -> backends.Backend:
        backend = _engine().backend_for(device_id)
        if backend is None:
            raise HTTPException(404, f"device '{device_id}' is not running")
        return backend

    def _pairable(device_id: str) -> backends.Pairable:
        backend = _running(device_id)
        if not isinstance(backend, backends.Pairable):
            raise HTTPException(400, f"device '{device_id}' does not need pairing")
        return backend

    def _learnable(device_id: str) -> backends.Learnable:
        backend = _running(device_id)
        if not isinstance(backend, backends.Learnable):
            raise HTTPException(400, f"device '{device_id}' cannot learn commands")
        return backend

    @app.get("/api/devices/{device_id}/commands", response_model=List[CommandInfo])
    async def get_device_commands(device_id: str) -> List[CommandInfo]:
        backend = _running(device_id)
        return [
            CommandInfo(
                name=c.name,
                label=c.label,
                description=c.description,
                params=c.params,
                repeatable=c.repeatable,
            )
            for c in await backend.commands()
        ]

    @app.get("/api/devices/{device_id}/entities", response_model=List[EntityInfo])
    async def get_device_entities(device_id: str, controllable_only: bool = True) -> List[EntityInfo]:
        """What a device could offer commands for, for picking from.

        Only Home Assistant needs this today: it is the one backend whose
        command list is not fixed but drawn from hundreds of entities that
        differ in every house, so the choice of which ones matter has to be
        made against a live list rather than typed. A backend with a fixed
        vocabulary has nothing to answer here and says so.

        `controllable_only` hides the domains that report rather than obey.
        The backend flags them rather than dropping them, so this stays the
        caller's decision.
        """
        backend = _running(device_id)
        catalogue = getattr(backend, "entities", None)
        if catalogue is None:
            raise HTTPException(
                400, f"device '{device_id}' has a fixed command list -- nothing to choose from"
            )
        try:
            found = await catalogue()
        except Exception as err:
            # The device is running but its equipment is not answering, which
            # is a 409 rather than a 500: nothing here is broken, the thing on
            # the other end is just not there.
            raise HTTPException(409, str(err))
        return [
            EntityInfo(**entry)
            for entry in found
            if not controllable_only or entry.get("controllable", True)
        ]

    @app.get("/api/devices/{device_id}/suggested_bindings")
    async def get_suggested_bindings(device_id: str) -> Dict[str, Any]:
        """A starting point for pointing the remote at this device.

        `bindings` is button key to command name, same as ever. `adjust` is
        separate because its value is not a command -- it is button key to
        `"up"`/`"down"`, for an `AdjustAction` that steps whatever the engine
        is focused on at press time rather than a command fixed now.
        Building the scene is left to the caller so it goes through
        `PUT /api/config` like any hand-made one, and gets the same
        validation.
        """
        backend = _running(device_id)
        return {"bindings": backend.suggested_bindings(), "adjust": backend.suggested_adjust()}

    @app.post("/api/devices/{device_id}/test")
    async def test_device_command(device_id: str, request: TestCommandRequest) -> Dict[str, Any]:
        """Fires one command immediately, so a device can be checked while being set up."""
        backend = _running(device_id)
        try:
            await backend.send(request.command, request.params)
        except Exception as err:
            return {"ok": False, "detail": str(err)}
        return {"ok": True, "detail": f"sent {request.command}"}

    # ------------------------------------------------------------------
    # Pairing
    #
    # Some equipment will not take orders from an unknown client until a human
    # has confirmed it on the device itself. That is a two-step conversation,
    # so it cannot be a field in the device form. Both routes work for any
    # backend implementing `backends.Pairable`, not just Android TV.
    # ------------------------------------------------------------------

    @app.post("/api/devices/{device_id}/pair/start")
    async def pair_start(device_id: str) -> Dict[str, Any]:
        """Asks the device to show its pairing code."""
        backend = _pairable(device_id)
        try:
            detail = await backend.pair_start()
        except Exception as err:
            return {"ok": False, "detail": str(err)}
        return {"ok": True, "detail": detail}

    @app.post("/api/devices/{device_id}/pair/finish")
    async def pair_finish(device_id: str, request: PairFinishRequest) -> Dict[str, Any]:
        """Completes pairing with the code the user read off the screen."""
        backend = _pairable(device_id)
        try:
            await backend.pair_finish(request.code)
        except Exception as err:
            return {"ok": False, "detail": str(err)}
        return {"ok": True, "detail": "paired"}

    # ------------------------------------------------------------------
    # Learning
    #
    # Teaching a device its own commands by capturing them off a remote --
    # currently only `IrBackend`, but any backend implementing
    # `backends.Learnable` gets these six routes for free. Polled rather
    # than pushed over the event socket, the same way `/api/radio/discover`
    # is: a dropped websocket mid-capture must not lose it.
    # ------------------------------------------------------------------

    def _learn_status(status) -> LearnStatusInfo:
        return LearnStatusInfo(
            state=status.state, detail=status.detail, decoded=status.decoded, pulses=status.pulses
        )

    @app.post("/api/devices/{device_id}/learn/start", response_model=LearnStatusInfo)
    async def learn_start(device_id: str, timeout: float = 20.0) -> LearnStatusInfo:
        """Begins listening for one command. Poll `GET .../learn` for progress."""
        backend = _learnable(device_id)
        try:
            status = await backend.learn_start(timeout)
        except backends.BackendError as err:
            raise HTTPException(409, str(err))
        return _learn_status(status)

    @app.get("/api/devices/{device_id}/learn", response_model=LearnStatusInfo)
    async def learn_status(device_id: str) -> LearnStatusInfo:
        return _learn_status(_learnable(device_id).learn_status())

    @app.post("/api/devices/{device_id}/learn/cancel", response_model=LearnStatusInfo)
    async def learn_cancel(device_id: str) -> LearnStatusInfo:
        return _learn_status(_learnable(device_id).learn_cancel())

    @app.post("/api/devices/{device_id}/learn/verify")
    async def learn_verify(device_id: str) -> Dict[str, Any]:
        """Replays the most recent capture, so it can be checked before it is named and saved."""
        backend = _learnable(device_id)
        try:
            await backend.learn_verify()
        except backends.BackendError as err:
            return {"ok": False, "detail": str(err)}
        return {"ok": True, "detail": "sent"}

    @app.post("/api/devices/{device_id}/learn/save", response_model=List[CommandInfo])
    async def learn_save(device_id: str, request: LearnSaveRequest) -> List[CommandInfo]:
        """Names the most recent capture and adds it to this device's commands."""
        backend = _learnable(device_id)
        try:
            await backend.learn_save(
                request.name, request.label, repeatable=request.repeatable, repeats=request.repeats
            )
        except backends.BackendError as err:
            raise HTTPException(409, str(err))
        return [
            CommandInfo(name=c.name, label=c.label, description=c.description, params=c.params, repeatable=c.repeatable)
            for c in await backend.commands()
        ]

    @app.delete("/api/devices/{device_id}/learn/{name}", response_model=List[CommandInfo])
    async def learn_forget(device_id: str, name: str) -> List[CommandInfo]:
        backend = _learnable(device_id)
        await backend.learn_forget(name)
        return [
            CommandInfo(name=c.name, label=c.label, description=c.description, params=c.params, repeatable=c.repeatable)
            for c in await backend.commands()
        ]

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    @app.post("/api/scenes/{scene_id}/activate")
    async def activate_scene(scene_id: str) -> Dict[str, Any]:
        engine = _engine()
        if runtime.config.scene(scene_id) is None:
            raise HTTPException(404, f"no such scene '{scene_id}'")
        await engine.activate_scene(scene_id)
        return {"active_scene": engine.active_scene}

    @app.post("/api/scenes/stop")
    async def stop_scene() -> Dict[str, Any]:
        await _engine().stop_scene()
        return {"active_scene": None}

    @app.post("/api/buttons/{key}/simulate")
    async def simulate(key: str, request: SimulateRequest = Body(default=SimulateRequest())) -> Dict[str, Any]:
        """Injects a press as if it came from the remote.

        Goes through the same event path as the radio rather than calling the
        binding directly, so a button that works here genuinely works.
        """
        _engine()  # 409 with an explanation, rather than a press into nothing
        event = runtime.simulate(key, request.kind)
        return {"key": key, "kind": request.kind, "signature": event.signature}

    # ------------------------------------------------------------------
    # Live events
    # ------------------------------------------------------------------

    @app.websocket("/api/events")
    async def events_socket(websocket: WebSocket) -> None:
        await websocket.accept()

        async def push() -> None:
            # Recent history first, so a page that just opened is not blank
            # until the next button press. The broker belongs to the runtime
            # rather than the hub, so this survives a restart -- that log is
            # what someone is reading in order to work out why they
            # restarted it.
            for event in runtime.broker.history[-50:]:
                await websocket.send_text(event.model_dump_json())
            async for event in runtime.broker.subscribe():
                await websocket.send_text(event.model_dump_json())

        sender = asyncio.create_task(push())
        try:
            # The client never sends anything; this is here purely so the
            # handler is parked somewhere a disconnect can wake it. Without
            # it, the only task blocked here is `sender`, sitting in
            # `broker.subscribe()` -- which nothing but a new hub event ever
            # wakes -- so a shutdown-triggered disconnect (uvicorn injects a
            # 1012 close into every open socket) goes unnoticed and the
            # server hangs waiting for this task to finish.
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("Event socket closed", exc_info=True)
        finally:
            sender.cancel()
            with contextlib.suppress(BaseException):
                await sender

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    if static_dir and Path(static_dir).is_dir():
        # Mounted last so it cannot shadow any /api route.
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="ui")

    return app
