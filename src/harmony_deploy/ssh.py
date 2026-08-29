"""One SSH session: run commands, read/write files, escalate with sudo.

Paramiko is imported lazily, inside `Connection.open`, so `harmony-deploy
push` (the existing HTTP path) never needs it installed -- see the
`deploy` extra in `pyproject.toml`. Everything else in `harmony_deploy`
that needs to run a remote command depends only on the `Runner` shape
below, not on this module or on paramiko directly, so `probe.py` and
`plan.py` are testable with a fake that never imports either.
"""

from __future__ import annotations

import shlex
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol


class SshError(RuntimeError):
    """Something about the connection itself, or a remote command, failed."""


class UnknownHostKey(SshError):
    """The host is not in `known_hosts` and the caller declined to trust it."""


class HostKeyChanged(SshError):
    """The host presented a different key than the one already trusted.

    Never auto-accepted -- this is exactly the situation host-key checking
    exists to catch, and the fix is for a person to work out why, not for
    this tool to shrug and continue.
    """


class AuthenticationFailed(SshError):
    """Key/agent auth -- or an explicitly given password -- was rejected.

    Split out from `SshError` specifically so a caller can retry with a
    password prompt on *this* failure and this one only: retrying an
    unreachable host or a rejected host key with a password would not help
    and would just waste the user's time typing it.
    """


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Runner(Protocol):
    """What `probe.py` and `plan.py` need from a connection -- nothing paramiko-specific.

    `Connection` below satisfies this structurally. Tests satisfy it with a
    plain fake that maps command strings to canned `CommandResult`s.
    """

    def run(self, command: str, *, timeout: float = 30.0) -> CommandResult: ...

    def sudo(self, command: str, *, timeout: float = 60.0) -> CommandResult: ...

    def read_file(self, remote_path: str) -> Optional[str]: ...


#: `(hostname, fingerprint) -> True to trust it`. Called only for a host
#: paramiko has never seen before -- a *changed* key is refused outright,
#: before this is ever consulted, by `paramiko.SSHClient.connect` itself
#: raising `BadHostKeyException`.
TrustPrompt = Callable[[str, str], bool]


def quote(path: str) -> str:
    return shlex.quote(path)


class Connection:
    """A live SSH + SFTP session. Construct with `Connection.open`, not directly."""

    def __init__(self, client, sftp, password: Optional[str]) -> None:
        self._client = client
        self._sftp = sftp
        # Held only to feed `sudo -S` on stdin for this session's lifetime --
        # never logged, never placed in a command line where `ps` could see it.
        self._password = password

    @classmethod
    def open(
        cls,
        host: str,
        user: str,
        *,
        password: Optional[str] = None,
        port: int = 22,
        trust_host: Optional[TrustPrompt] = None,
        timeout: float = 15.0,
    ) -> "Connection":
        try:
            import paramiko
        except ImportError as err:
            raise SshError('SSH deploy needs paramiko: pip install -e ".[deploy]"') from err

        class _TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key) -> None:
                raw = key.get_fingerprint().hex()
                fingerprint = ":".join(raw[i : i + 2] for i in range(0, len(raw), 2))
                if trust_host is None or not trust_host(hostname, fingerprint):
                    raise UnknownHostKey(f"{hostname} presented an unrecognised key ({fingerprint})")
                client.get_host_keys().add(hostname, key.get_name(), key)

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(_TrustOnFirstUse())

        try:
            client.connect(
                host,
                port=port,
                username=user,
                password=password,
                look_for_keys=True,
                allow_agent=True,
                timeout=timeout,
            )
        except paramiko.BadHostKeyException as err:
            raise HostKeyChanged(f"host key for {host} has changed -- refusing to connect ({err})") from err
        except (UnknownHostKey, HostKeyChanged):
            raise
        except paramiko.SSHException as err:
            # Covers both a rejected key/password (`AuthenticationException`,
            # a subclass) *and* the plain `SSHException("No authentication
            # methods available")` paramiko raises instead when there was
            # nothing to even try -- no password given, no key files found,
            # no agent running. Both mean the same thing to a caller: try a
            # password. Distinguishing them would only matter if the retry
            # itself needed to behave differently, and it does not.
            raise AuthenticationFailed(f"authentication failed for {user}@{host}: {err}") from err
        except (socket.error, OSError) as err:
            raise SshError(f"could not connect to {user}@{host}: {err}") from err

        # Only reached once a connection is actually established, so a
        # rejected host key or a failed login never gets written to disk.
        try:
            client.save_host_keys(str(Path.home() / ".ssh" / "known_hosts"))
        except OSError:
            pass  # best-effort; a read-only known_hosts should not fail the connection

        sftp = client.open_sftp()
        return cls(client, sftp, password)

    def run(self, command: str, *, timeout: float = 30.0) -> CommandResult:
        _stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return CommandResult(stdout.channel.recv_exit_status(), out, err)

    def sudo(self, command: str, *, timeout: float = 60.0) -> CommandResult:
        """Runs `command` as root via `sudo -S`, feeding the password on stdin.

        `-p ''` suppresses sudo's own "[sudo] password for pi:" prompt text,
        which would otherwise land in `stdout`/`stderr` and have to be
        stripped back out by every caller.
        """
        stdin, stdout, stderr = self._client.exec_command(f"sudo -S -p '' {command}", timeout=timeout)
        if self._password is not None:
            stdin.write(self._password + "\n")
            stdin.flush()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return CommandResult(stdout.channel.recv_exit_status(), out, err)

    def read_file(self, remote_path: str) -> Optional[str]:
        """Text contents of `remote_path`, or `None` if it does not exist. Assumes UTF-8."""
        data = self.read_bytes(remote_path)
        return None if data is None else data.decode("utf-8")

    def read_bytes(self, remote_path: str) -> Optional[bytes]:
        try:
            with self._sftp.open(remote_path, "rb") as f:
                return f.read()
        except IOError:
            return None

    def put(self, local_path: "Path | str", remote_path: str) -> None:
        self._sftp.put(str(local_path), remote_path)

    def put_bytes(self, data: bytes, remote_path: str) -> None:
        with self._sftp.open(remote_path, "wb") as f:
            f.write(data)

    def close(self) -> None:
        self._sftp.close()
        self._client.close()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
