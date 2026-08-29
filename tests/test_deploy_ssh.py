"""ssh.py, with no real network or real paramiko connection in sight.

Most of this drives `Connection` directly through a fake paramiko-shaped
client/sftp pair. `Connection.open`'s own exception mapping -- which
paramiko failure becomes which of this module's exceptions -- is tested
separately by installing a minimal fake `paramiko` module into
`sys.modules`, since that logic lives inside `open` itself and paramiko is
imported lazily there specifically so this file never needs the real
package installed. What all of it is really pinning down: the password
never appears on a command line where `ps` could see it, `CommandResult.ok`
reflects the exit code, file read/write go through SFTP correctly, and a
device with no SSH keys registered is treated as "needs a password" rather
than silently failing to connect at all.
"""

from __future__ import annotations

import sys

import pytest

from harmony_deploy.ssh import AuthenticationFailed, CommandResult, Connection, HostKeyChanged


class FakeChannel:
    def __init__(self, exit_status: int) -> None:
        self._exit_status = exit_status

    def recv_exit_status(self) -> int:
        return self._exit_status


class FakeStream:
    def __init__(self, data: bytes, channel: "FakeChannel | None" = None) -> None:
        self._data = data
        self.channel = channel

    def read(self) -> bytes:
        return self._data


class FakeStdin:
    def __init__(self) -> None:
        self.written: list = []

    def write(self, data: str) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass


class FakeClient:
    def __init__(self, exit_status: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.exec_command_calls: list = []
        self.last_stdin: "FakeStdin | None" = None
        self._exit_status = exit_status
        self._stdout = stdout
        self._stderr = stderr

    def exec_command(self, command: str, timeout: "float | None" = None):
        self.exec_command_calls.append(command)
        stdin = FakeStdin()
        self.last_stdin = stdin
        stdout = FakeStream(self._stdout, channel=FakeChannel(self._exit_status))
        stderr = FakeStream(self._stderr)
        return stdin, stdout, stderr


class FakeSftpFile:
    def __init__(self, store: dict, path: str, data: bytes = b"") -> None:
        self._store = store
        self._path = path
        self._buffer = bytearray(data)

    def read(self) -> bytes:
        return bytes(self._buffer)

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)
        self._store[self._path] = bytes(self._buffer)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass


class FakeSftp:
    def __init__(self, files: "dict | None" = None) -> None:
        self.files = files or {}
        self.puts: list = []

    def open(self, path: str, mode: str):
        if "r" in mode:
            if path not in self.files:
                raise IOError(f"no such file: {path}")
            return FakeSftpFile(self.files, path, self.files[path])
        return FakeSftpFile(self.files, path)

    def put(self, local_path: str, remote_path: str) -> None:
        self.puts.append((local_path, remote_path))


def test_run_does_not_touch_stdin():
    client = FakeClient(exit_status=0, stdout=b"hello\n")
    conn = Connection(client, FakeSftp(), password="hunter2")

    result = conn.run("echo hello")

    assert result == CommandResult(0, "hello\n", "")
    assert result.ok
    assert client.last_stdin.written == []


def test_a_nonzero_exit_is_not_ok():
    client = FakeClient(exit_status=1, stderr=b"boom")
    conn = Connection(client, FakeSftp(), password=None)
    result = conn.run("false")
    assert result.ok is False
    assert result.stderr == "boom"


def test_sudo_sends_the_password_on_stdin_never_in_the_command_line():
    client = FakeClient(exit_status=0)
    conn = Connection(client, FakeSftp(), password="hunter2")

    conn.sudo("systemctl restart harmony-hub")

    (command,) = client.exec_command_calls
    assert "hunter2" not in command  # the whole point: never in argv, never in a log line
    assert command == "sudo -S -p '' systemctl restart harmony-hub"
    assert client.last_stdin.written == ["hunter2\n"]


def test_sudo_with_no_password_writes_nothing_to_stdin():
    """Passwordless sudo (a NOPASSWD sudoers entry) needs no password fed at all."""
    client = FakeClient(exit_status=0)
    conn = Connection(client, FakeSftp(), password=None)

    conn.sudo("systemctl restart harmony-hub")

    assert client.last_stdin.written == []


def test_read_file_returns_none_for_a_missing_path():
    conn = Connection(FakeClient(), FakeSftp(), password=None)
    assert conn.read_file("/does/not/exist") is None


def test_read_file_decodes_utf8_text():
    sftp = FakeSftp(files={"/etc/motd": b"hello pi\n"})
    conn = Connection(FakeClient(), sftp, password=None)
    assert conn.read_file("/etc/motd") == "hello pi\n"


def test_read_bytes_round_trips_binary_data_like_an_update_token():
    token = bytes(range(256))[:32]
    sftp = FakeSftp(files={"/data/update_token": token})
    conn = Connection(FakeClient(), sftp, password=None)
    assert conn.read_bytes("/data/update_token") == token


def test_put_bytes_writes_through_sftp():
    sftp = FakeSftp()
    conn = Connection(FakeClient(), sftp, password=None)
    conn.put_bytes(b"unit file contents", "/etc/systemd/system/harmony-hub.service")
    assert sftp.files["/etc/systemd/system/harmony-hub.service"] == b"unit file contents"


def test_context_manager_closes_the_connection():
    closed = []

    class _Sftp(FakeSftp):
        def close(self):
            closed.append("sftp")

    class _Client(FakeClient):
        def close(self):
            closed.append("client")

    with Connection(_Client(), _Sftp(), password=None):
        pass

    assert closed == ["sftp", "client"]


class _FakeParamikoModule:
    """Just enough of paramiko's surface for Connection.open to run against, with no real network.

    Installed into sys.modules so the lazy `import paramiko` inside
    `Connection.open` picks this up instead of the real package -- lets the
    exception-mapping logic in `open` itself be tested without a device.
    """

    class SSHException(Exception):
        pass

    class AuthenticationException(SSHException):
        pass

    class BadHostKeyException(SSHException):
        def __init__(self):
            super().__init__("host key mismatch")

    class MissingHostKeyPolicy:
        def missing_host_key(self, client, hostname, key):
            raise NotImplementedError

    def __init__(self, connect_error=None):
        self._connect_error = connect_error
        self.client = None

    def SSHClient(self):
        outer = self

        class _Client:
            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, *args, **kwargs):
                if outer._connect_error is not None:
                    raise outer._connect_error

            def save_host_keys(self, path):
                pass

            def open_sftp(self):
                return FakeSftp()

            def close(self):
                pass

        outer.client = _Client()
        return outer.client


def _install_fake_paramiko(monkeypatch, connect_error):
    fake = _FakeParamikoModule(connect_error)
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    return fake


def test_no_authentication_methods_available_is_treated_as_an_auth_failure(monkeypatch):
    """The regression this guards: paramiko raises a plain SSHException (not

    AuthenticationException) when no password was given and no key/agent
    auth had anything to try at all -- no keys on disk, no agent running.
    A caller retrying only on `AuthenticationFailed` (see `setup.connect`)
    must still see this case as one, or a user with no SSH keys set up
    never gets asked for a password at all.
    """
    fake = _install_fake_paramiko(
        monkeypatch, connect_error=_FakeParamikoModule.SSHException("No authentication methods available")
    )

    with pytest.raises(AuthenticationFailed):
        Connection.open("10.0.0.1", "pi", password=None)


def test_a_genuinely_rejected_password_is_also_an_auth_failure(monkeypatch):
    _install_fake_paramiko(monkeypatch, connect_error=_FakeParamikoModule.AuthenticationException())
    with pytest.raises(AuthenticationFailed):
        Connection.open("10.0.0.1", "pi", password="wrong")


def test_a_changed_host_key_is_never_treated_as_an_auth_failure(monkeypatch):
    _install_fake_paramiko(monkeypatch, connect_error=_FakeParamikoModule.BadHostKeyException())
    with pytest.raises(HostKeyChanged):
        Connection.open("10.0.0.1", "pi", password="whatever")


def test_a_successful_connection_needs_no_exception_mapping_at_all(monkeypatch):
    _install_fake_paramiko(monkeypatch, connect_error=None)
    conn = Connection.open("10.0.0.1", "pi", password="hunter2")
    assert conn is not None
