import asyncio
import json
import logging
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hevy_mcp.auth import Session, SessionManager
from hevy_mcp.config import SettingsStore
from hevy_mcp.errors import AuthenticationError
from hevy_mcp.logging import RedactionFilter, redact, redact_data


class Refresher:
    def __init__(self):
        self.calls = 0

    def refresh(self, session: Session):
        self.calls += 1
        assert session.access_token == "old"
        assert session.refresh_token == "refresh"
        return Session(
            access_token="new-secret",
            refresh_token="new-refresh",
            user_id="u1",
            email="a@b",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


@pytest.mark.asyncio
async def test_expired_token_refresh_is_persisted(tmp_path) -> None:
    mgr = SessionManager(tmp_path, refresher=Refresher())
    mgr.save(
        Session(
            access_token="old",
            refresh_token="refresh",
            user_id="u1",
            email="a@b",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    session = await mgr.valid_session()
    assert session.access_token == "new-secret"
    assert json.loads((tmp_path / "credentials.json").read_text())["access_token"] == "new-secret"
    assert (tmp_path / "credentials.json").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_concurrent_expired_session_refreshes_once(tmp_path) -> None:
    refresher = Refresher()
    mgr = SessionManager(tmp_path, refresher=refresher)
    mgr.save(
        Session(
            access_token="old",
            refresh_token="refresh",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    sessions = await __import__("asyncio").gather(*[mgr.valid_session() for _ in range(8)])
    assert refresher.calls == 1
    assert {session.access_token for session in sessions} == {"new-secret"}


@pytest.mark.asyncio
async def test_distinct_managers_for_same_store_coalesce_refresh(tmp_path) -> None:
    refresher = Refresher()
    first = SessionManager(tmp_path, refresher=refresher)
    second = SessionManager(tmp_path, refresher=refresher)
    first.save(
        Session(
            access_token="old",
            refresh_token="refresh",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )

    sessions = await asyncio.gather(first.valid_session(), second.valid_session())

    assert refresher.calls == 1
    assert {session.access_token for session in sessions} == {"new-secret"}


@pytest.mark.skipif(sys.platform != "linux", reason="fcntl process lock is Linux-specific")
def test_distinct_processes_for_same_store_refresh_once(tmp_path, child_command, child_env) -> None:
    manager = SessionManager(tmp_path)
    manager.save(
        Session(
            access_token="old",
            refresh_token="refresh",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    script = textwrap.dedent(
        """
        import asyncio
        import os
        import sys
        import time
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        import fcntl
        from hevy_mcp.auth import Session, SessionManager

        root = Path(sys.argv[1])
        barrier = root / "start"
        calls = root / "refresh-calls"
        original_flock = fcntl.flock

        def observed_flock(fd, operation):
            if operation == fcntl.LOCK_EX:
                (root / f"lock-attempt-{os.getpid()}").touch()
            return original_flock(fd, operation)

        fcntl.flock = observed_flock

        class Refresher:
            def refresh(self, session):
                fd = os.open(calls, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, f"{os.getpid()}\\n".encode())
                finally:
                    os.close(fd)
                deadline = time.monotonic() + 5
                while len(list(root.glob("lock-attempt-*"))) < 2:
                    if time.monotonic() > deadline:
                        raise AssertionError("second process did not attempt flock")
                    time.sleep(0.01)
                time.sleep(0.1)
                return Session(
                    access_token=f"new-{os.getpid()}",
                    refresh_token="new-refresh",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )

        manager = SessionManager(root, refresher=Refresher())
        stale = manager.load()
        assert stale.access_token == "old"
        (root / f"ready-{os.getpid()}").touch()
        deadline = time.monotonic() + 5
        while not barrier.exists():
            if time.monotonic() > deadline:
                raise AssertionError("parent did not release barrier")
            time.sleep(0.01)
        session = asyncio.run(manager.refresh(expected_access_token=stale.access_token))
        print(session.access_token)
        """
    )
    processes = [
        subprocess.Popen(
            child_command(script, tmp_path),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        deadline = time.monotonic() + 5
        while len(list(tmp_path.glob("ready-*"))) < 2:
            assert time.monotonic() < deadline, "children did not load stale credentials"
            time.sleep(0.01)
        (tmp_path / "start").touch()
        outputs = [process.communicate(timeout=10) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    assert [process.returncode for process in processes] == [0, 0], outputs
    assert len(list(tmp_path.glob("lock-attempt-*"))) == 2
    assert len((tmp_path / "refresh-calls").read_text().splitlines()) == 1
    assert len({stdout.strip() for stdout, _ in outputs}) == 1
    lock_path = tmp_path / "credentials.json.lock"
    assert lock_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_refresh_failure_preserves_credentials_byte_for_byte(tmp_path) -> None:
    class FailingRefresher:
        def refresh(self, session):
            raise AuthenticationError("refresh failed")

    manager = SessionManager(tmp_path, refresher=FailingRefresher())
    manager.save(
        Session(
            access_token="old",
            refresh_token="refresh",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    before = manager.path.read_bytes()

    with pytest.raises(AuthenticationError, match="refresh failed"):
        await manager.valid_session()

    assert manager.path.read_bytes() == before


def test_logout_removes_local_credentials_and_is_idempotent(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    manager.save(Session(access_token="access", refresh_token="refresh"))

    manager.logout()
    manager.logout()

    assert not manager.path.exists()


def test_browser_login_disables_upstream_credential_store(tmp_path, monkeypatch) -> None:
    calls = []

    class Tokens:
        access_token = "access"
        refresh_token = "refresh"
        user_id = "u"
        expires_at = None

    def login_via_browser(**kwargs):
        calls.append(kwargs)
        return Tokens(), "person@example.com"

    monkeypatch.setattr("hevy_unofficial.login_via_browser", login_via_browser)
    SessionManager(tmp_path).login_browser("person@example.com")
    assert calls == [{"email": "person@example.com", "save": False}]
    assert list(tmp_path.iterdir()) == [tmp_path / "credentials.json"]


def test_browser_login_wraps_missing_playwright_without_secret_details(
    tmp_path, monkeypatch
) -> None:
    def fail(**kwargs):
        raise RuntimeError("Executable doesn't exist at /secret/browser/path")

    monkeypatch.setattr("hevy_unofficial.login_via_browser", fail)
    with pytest.raises(AuthenticationError, match="playwright install chromium") as exc:
        SessionManager(tmp_path).login_browser("person@example.com")
    assert "/secret/browser/path" not in str(exc.value)


def test_browser_extra_is_an_install_dependency() -> None:
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert '"hevy-unofficial[browser]==0.0.2a0"' in pyproject
    assert '"hevy-unofficial[core]==0.0.2a0"' not in pyproject


def test_redaction_hides_tokens_passwords_and_auth_headers() -> None:
    text = redact("Authorization: Bearer abc123 access_token=secret password=hunter2 cookie=xyz")
    assert (
        "abc123" not in text
        and "secret" not in text
        and "hunter2" not in text
        and "xyz" not in text
    )
    rec = logging.LogRecord("x", 20, "", 1, "refresh_token=%s", ("verysecret",), None)
    assert RedactionFilter().filter(rec)
    assert "verysecret" not in rec.getMessage()


def test_redaction_recurses_json_headers_query_and_cookies() -> None:
    payload = {
        "nested": [{"access_token": "sekrit"}],
        "headers": {"aUtHoRiZaTiOn": "Bearer bearer-secret", "Cookie": "sid=cookie-secret"},
        "url": "https://example.test/x?refresh_token=query-secret&safe=yes",
    }
    cleaned = redact_data(payload)
    rendered = json.dumps(cleaned)
    for secret in ["sekrit", "bearer-secret", "cookie-secret", "query-secret"]:
        assert secret not in rendered
    assert cleaned["nested"][0]["access_token"] == "[REDACTED]"
    raw_json = redact('{"access_token": "json-secret", "safe": true}')
    assert "json-secret" not in raw_json and "safe" in raw_json


@pytest.mark.parametrize(
    "value",
    [
        '{"refresh_token": "sekrit-001"}',
        "{'password': 'sekrit-001'}",
        "{'refreshToken': 'sekrit-001'}",
        "{'clientSecret': 'sekrit-001'}",
        "AUTHORIZATION: Bearer ***",
        "Authorization: Basic sekrit-001",
        "Cookie: sid=sekrit-001",
        "https://example.test/x?access_token=sekrit-001&visible=yes",
        "https://example.test/x?auth2.0-token=sekrit-001&visible=yes",
        "accessToken=sekrit-001",
        "refresh-token=sekrit-001",
        "client_secret=sekrit-001",
        "auth2.0-token=sekrit-001",
        '"access_token": "sekrit-001\ncontinued"',
        '"clientSecret": "sekrit-001\ncontinued"',
    ],
)
def test_redaction_table_removes_secret_from_unstructured_values(value) -> None:
    assert "sekrit-001" not in redact(value)


def test_redaction_does_not_corrupt_safe_similar_words() -> None:
    safe = (
        "token_count=4 cookie_recipe=oatmeal authorization_count=2 "
        "client_secretary=Sam refreshTokenCount=1 https://example.test/?visible=yes"
    )
    assert redact(safe) == safe


@pytest.mark.parametrize(
    "header",
    [
        "Cookie: sid=FIRST-SECRET; sessionid=SECOND-SECRET",
        "Set-Cookie: sid=FIRST-SECRET; Secure, sessionid=SECOND-SECRET",
        "Authorization: Digest username=safe, response=DIGEST-SECRET",
    ],
)
def test_unstructured_credential_header_redacts_everything_through_line_end(header) -> None:
    cleaned = redact(f"request headers\n{header}\nX-Request-ID: visible")
    assert "FIRST-SECRET" not in cleaned
    assert "SECOND-SECRET" not in cleaned
    assert "DIGEST-SECRET" not in cleaned
    assert "X-Request-ID: visible" in cleaned


def test_structured_redaction_normalizes_common_secret_key_spellings() -> None:
    payload = {
        "accessToken": "one",
        "refresh-token": "two",
        "auth2.0-token": "three",
        "client_secret": "four",
        "clientSecret": "five",
        "Authorization": "Digest six",
        "safe": "visible",
    }

    assert redact_data(payload) == {
        **{key: "[REDACTED]" for key in payload if key != "safe"},
        "safe": "visible",
    }


def test_settings_backend_is_explicit_and_atomic(tmp_path) -> None:
    store = SettingsStore(tmp_path)
    assert store.load().workout_delete_backend is None
    store.set_delete_backend("v2")
    assert store.load().workout_delete_backend == "v2"
    assert (tmp_path / "config.yaml").stat().st_mode & 0o777 == 0o600
