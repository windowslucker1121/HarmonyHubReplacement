"""Remote update: pushing new code (Python + the built web UI) to an already-deployed, running hub.

Config never travels -- see `manifest.ALLOWED_PATTERNS`, the actual
enforcement point. What does travel is a signed, allowlisted tarball of
`harmony_hub`/`harmony_receiver` source plus the built Flutter web output,
installed into its own versioned release directory and activated by
flipping a pointer in `update_state.json` rather than overwriting anything
in place -- so a bad deploy has somewhere to roll back to, and a good one
never has to touch `hub_settings.json`, `hub_config.json`, `buttons.json`,
or `credentials/`.

Pieces, dev machine to device:

* `bundle.py`   -- builds the tarball (dev machine only).
* `manifest.py` -- the allowlist and the manifest schema; imported by both
  ends so they agree on what a bundle is without trusting each other.
* `auth.py`     -- HMAC signing over a nonce and the bundle's content hash.
  The shared token itself never crosses the wire.
* `extract.py`  -- unpacks a bundle assuming it is hostile even though the
  signature already checked out.
* `installer.py`-- stage, install dependencies, smoke-test, activate. No
  FastAPI import, so it is tested without a TestClient.
* `state.py`    -- which release is `current`/`previous`, and install
  history. Lives in `data/`, not the code tree, so an update can never
  touch it.
* `confirm.py`  -- marks a trial release good from *inside* the process it
  is running, once it has proven it can actually serve.
* `launcher.py` -- the one file an update never replaces. Decides which
  release to boot, and rolls back a release that keeps failing to start.
"""

from __future__ import annotations
