"""harmony_hub: scenes, bindings, and device control on top of a Harmony remote.

`harmony_receiver` answers "which button was pressed". This package answers
"and what should happen". The two are kept apart so the RF work stays usable
on its own, and so everything here can be developed and tested without a
radio attached.

Shape of the thing::

    remote ──► EventSource ──► SceneEngine ──► Backend
                                  │              (http / shell / virtual / yours)
                              active scene
                              decides what each
                              button means

`HubRuntime` supervises all of that as a restartable unit, keeping settings,
configuration and the event broker alive across restarts. The web layer sits
above it and never restarts, so a hub that cannot start is something the
settings page reports rather than something that stops the page loading.

A *scene* is a context, not a macro: while it is active it decides what
every button does, falling back to another scene -- the configured
`global_scene` -- for anything it does not bind. A *backend* is any way of
reaching equipment; the engine only ever
calls `send()` on one, so new kinds of device need no changes here. Backends
in other packages are discovered through the `harmony_hub.backends` entry
point group.

Typical embedded use::

    from harmony_hub import SceneEngine, config
    from harmony_receiver import ButtonMap

    engine = SceneEngine(config.load(), ButtonMap.load("buttons.json"))
    await engine.start()
    await engine.handle(event)          # from any EventSource
"""

from __future__ import annotations

from . import backends, config, settings
from .engine import SceneEngine
from .events import EventBroker, HubEvent
from .models import (
    Action,
    Binding,
    DelayAction,
    Device,
    DeviceAction,
    HubConfig,
    PowerPolicy,
    Scene,
    SceneAction,
)
from .runtime import HubRuntime
from .service import HubService
from .settings import HubSettings
from .sources import EventSource, ManualSource, RadioSource, ReplaySource, build_source

__version__ = "0.1.0"

__all__ = [
    "Action",
    "Binding",
    "DelayAction",
    "Device",
    "DeviceAction",
    "EventBroker",
    "EventSource",
    "HubConfig",
    "HubEvent",
    "HubRuntime",
    "HubService",
    "HubSettings",
    "ManualSource",
    "PowerPolicy",
    "RadioSource",
    "ReplaySource",
    "Scene",
    "SceneAction",
    "SceneEngine",
    "backends",
    "build_source",
    "config",
    "settings",
]
