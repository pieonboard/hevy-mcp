import asyncio
import json
import os
import socket
import stat
import subprocess

import pytest
from pytest_socket import SocketBlockedError

from hevy_mcp.auth import Session, SessionManager
from hevy_mcp.config import SettingsStore, atomic_write, config_dir, secure_dir
from hevy_mcp.errors import AuthenticationError, ConfigurationError


def test_parent_and_child_tcp_are_denied(child_command, child_env):
    with pytest.warns(UserWarning, match="socket.socket"), pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    code = """
import socket
from pytest_socket import SocketBlockedError
try:
    socket.create_connection(('127.0.0.1', 9))
except SocketBlockedError:
    print('denied')
else:
    raise AssertionError('TCP enabled in child')
"""
    result = subprocess.run(
        child_command(code), env=child_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "denied"


def test_default_store_and_child_use_only_temporary_root(tmp_path, child_command, child_env):
    assert config_dir() == tmp_path / "hevy"
    assert SessionManager().path == tmp_path / "hevy" / "credentials.json"
    code = """
import json
from hevy_mcp.auth import SessionManager
from hevy_mcp.errors import AuthenticationError
manager = SessionManager()
try:
    manager.load()
except AuthenticationError:
    print(json.dumps({'path': str(manager.path), 'absent': True}))
"""
    result = subprocess.run(
        child_command(code), env=child_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "path": str(tmp_path / "hevy" / "credentials.json"),
        "absent": True,
    }


def test_secure_dir_tightens_existing_directory(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    assert secure_dir(root) == root
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_atomic_write_fsyncs_before_publication_and_directory_after(tmp_path, monkeypatch):
    path = tmp_path / "private" / "state"
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    def replace(source, target):
        assert stat.S_IMODE(os.stat(source).st_mode) == 0o600
        assert open(source).read() == "complete\n"
        events.append("publish")
        real_replace(source, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    atomic_write(path, "complete\n")
    assert events == ["file", "publish", "directory"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.iterdir()) == [path]


def test_exclusive_publication_preserves_existing_bytes(tmp_path):
    path = tmp_path / "snapshot.json"
    atomic_write(path, "original", exclusive=True)
    with pytest.raises(FileExistsError):
        atomic_write(path, "replacement", exclusive=True)
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_prepublication_fsync_leaves_original_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state"
    atomic_write(path, "original")

    def fail(fd):
        raise OSError("disk fsync failed")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="fsync"):
        atomic_write(path, "replacement")
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]


def test_directory_fsync_failure_does_not_claim_success(tmp_path, monkeypatch):
    real_fsync = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(OSError, match="directory"):
        atomic_write(tmp_path / "state", "published")
    assert (tmp_path / "state").read_bytes() == b"published"
    assert list(tmp_path.iterdir()) == [tmp_path / "state"]


@pytest.mark.parametrize("wrapped", [True, False])
def test_settings_accepts_historical_yaml_shapes(tmp_path, wrapped):
    (tmp_path / "config.yaml").write_text(
        "hevy:\n  workout_delete_backend: v1\n" if wrapped else "workout_delete_backend: v1\n"
    )
    assert SettingsStore(tmp_path).load().workout_delete_backend == "v1"


def test_settings_rejects_invalid_backend_without_publication(tmp_path):
    with pytest.raises(ConfigurationError):
        SettingsStore(tmp_path).set_delete_backend("guess")
    assert not (tmp_path / "config.yaml").exists()


@pytest.mark.parametrize("data", ["[broken yaml", "- not-a-map"])
def test_settings_malformed_file_fails_closed(tmp_path, data):
    (tmp_path / "config.yaml").write_text(data)
    with pytest.raises(ConfigurationError):
        SettingsStore(tmp_path).load()


@pytest.mark.asyncio
async def test_refresh_keeps_identity_and_reuses_already_rotated_token(tmp_path):
    class Refresh:
        calls = 0

        def refresh(self, session):
            self.calls += 1
            return Session(access_token="new", refresh_token="rotated")

    refresher = Refresh()
    manager = SessionManager(tmp_path, refresher)
    manager.save(Session(access_token="old", refresh_token="refresh", user_id="u", email="a@b"))
    first = await manager.refresh(expected_access_token="old")
    second = await manager.refresh(expected_access_token="old")
    assert first == second
    assert second.user_id == "u" and second.email == "a@b"
    assert refresher.calls == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX lock contract")
def test_refresh_lock_rejects_symlink_without_touching_target(tmp_path):
    manager = SessionManager(tmp_path)
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    manager.lock_path.symlink_to(target)
    with pytest.raises(OSError):
        asyncio.run(manager.refresh())
    assert target.read_bytes() == b"untouched"


def test_corrupt_credentials_fail_without_rewrite(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_bytes(b'{"access_token": "broken-secret"')
    with pytest.raises(AuthenticationError, match="Invalid local session"):
        SessionManager(tmp_path).load()
    assert path.read_bytes() == b'{"access_token": "broken-secret"'


def test_child_refresh_failure_preserves_original_bytes(tmp_path, child_command, child_env):
    manager = SessionManager(tmp_path)
    original = b'{ "access_token": "old", "refresh_token": "refresh", "expires_at": "2000-01-01T00:00:00Z" }\n'
    manager.path.write_bytes(original)
    code = """
import asyncio
import sys
from pathlib import Path
from hevy_mcp.auth import SessionManager
from hevy_mcp.errors import AuthenticationError
class Fail:
    def refresh(self, session):
        raise AuthenticationError('fixed synthetic failure')
try:
    asyncio.run(SessionManager(Path(sys.argv[1]), Fail()).valid_session())
except AuthenticationError:
    print('failed safely')
else:
    raise AssertionError('refresh unexpectedly succeeded')
"""
    result = subprocess.run(
        child_command(code, tmp_path), env=child_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "failed safely"
    assert manager.path.read_bytes() == original


def test_imports_and_factory_perform_no_account_io(tmp_path, child_command, child_env):
    code = """
import sys
from pathlib import Path
import hevy_unofficial
from hevy_mcp.auth import SessionManager

def forbidden(*args, **kwargs):
    raise AssertionError('account IO during import/construction')
hevy_unofficial.HevyClient = forbidden
hevy_unofficial.login_via_browser = forbidden
SessionManager.load = forbidden
SessionManager.login_browser = forbidden
import hevy_mcp.cli
import hevy_mcp.results
import hevy_mcp.runtime
from hevy_mcp.server import create_server
assert create_server()
assert not Path(sys.argv[1]).exists()
assert 'hevy_mcp.service' in sys.modules
assert 'hevy_mcp.deletion' in sys.modules
print('inert')
"""
    result = subprocess.run(
        child_command(code, tmp_path / "hevy"),
        env=child_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "inert"
