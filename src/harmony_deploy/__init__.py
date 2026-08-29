"""harmony-deploy: builds a code bundle on the dev machine and pushes it to a running hub.

The other half of `harmony_hub.update` -- that package verifies and installs
a bundle on the device; this one builds and signs it on whichever machine
you actually write code on. Kept as its own top-level package rather than
inside `harmony_hub` because it is dev tooling with a different dependency
story (it shells out to `pytest` and `flutter`, neither of which the device
needs), even though it reuses `harmony_hub.update.bundle`/`.auth`/`.manifest`
directly rather than re-implementing them.
"""

from __future__ import annotations
