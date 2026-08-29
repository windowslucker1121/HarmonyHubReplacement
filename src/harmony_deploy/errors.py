"""The one error type harmony-deploy's CLI catches at the top level.

Every subsystem here (bundling, HTTP push, SSH provisioning) raises its own
specific exceptions internally; each entry point in cli.py is responsible
for turning those into a DeployError with a message meant to be read by a
person, not parsed. Kept in its own module with no other imports so
nothing importing it can create a dependency cycle with the modules that
need it.
"""

from __future__ import annotations


class DeployError(RuntimeError):
    """Something is wrong enough that the command should stop, with a message meant to be read, not parsed."""
