"""Polling /api/version until a device reports the build just sent to it.

Shared by the HTTP push path and SSH-provisioned setup -- both end the same
way: wait for the hub to come back and say what it is actually running,
rather than trusting that a request succeeding means the hub did too.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

from .errors import DeployError


def wait_for_version(base_url: str, expected_build_id: str, deadline_seconds: float = 120.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            response = httpx.get(base_url.rstrip("/") + "/api/version", timeout=5.0)
            response.raise_for_status()
            info = response.json()
        except Exception as err:  # the device is mid-restart; expected, not a failure
            last_error = err
            continue
        if info.get("build_id") == expected_build_id:
            print(f"Live on {expected_build_id}.")
            return
        if info.get("previous") == expected_build_id:
            raise DeployError(f"the device rolled back -- it is now running {info.get('build_id')} again")
    raise DeployError(f"timed out waiting for the device to come back (last error: {last_error})")
