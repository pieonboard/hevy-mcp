import json
import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from hevy_mcp.auth import Session, SessionManager


@pytest.fixture
def runner(monkeypatch):
    # Doctor's browser check is an explicit dry-run subprocess in production.
    def dry_run(command, **kwargs):
        assert command[1:] == ["-m", "playwright", "install", "--dry-run", "chromium"]
        return SimpleNamespace(stdout="Install location: /nonexistent/chromium")

    monkeypatch.setattr("subprocess.run", dry_run)
    return CliRunner()


def app():
    from hevy_mcp.cli import app

    return app


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["serve", "--help"],
        ["auth", "login", "--help"],
        ["doctor", "--help"],
        ["test-delete-backend", "--help"],
    ],
)
def test_cli_help_keeps_names_and_flags(runner, arguments):
    result = runner.invoke(app(), arguments)
    assert result.exit_code == 0
    for flag in {
        "serve": ["--log-level"],
        "auth": ["--email"],
        "doctor": ["--live"],
        "test-delete-backend": ["--backend", "--workout-id", "--title", "--started-at"],
    }.get(arguments[0], []):
        assert flag in result.stdout


def test_serve_wires_fastmcp_and_preserves_log_level(runner, monkeypatch):
    calls = []
    monkeypatch.setattr("hevy_mcp.server.serve", lambda **kwargs: calls.append(kwargs))
    result = runner.invoke(app(), ["serve", "--log-level", "DEBUG"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls == [{"log_level": "DEBUG"}]


def test_status_and_idempotent_logout_json(runner):
    assert runner.invoke(app(), ["auth", "status"]).exit_code == 1
    SessionManager().save(
        Session(
            access_token="token-secret", refresh_token="refresh-secret", user_id="u1", email="a@b"
        )
    )
    result = runner.invoke(app(), ["auth", "status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "authenticated": True,
        "user_id": "u1",
        "email": "a@b",
        "expires_at": None,
        "refreshable": True,
        "expired": False,
    }
    assert "secret" not in result.stdout
    for _ in range(2):
        result = runner.invoke(app(), ["auth", "logout"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "authenticated": False,
            "local_session_removed": True,
            "remote_revoked": False,
        }


def test_auth_login_retains_email_and_safe_json(runner, monkeypatch):
    def fake_login(self, email):
        assert email == "a@b"
        session = Session(
            access_token="login-secret", refresh_token="refresh-secret", user_id="u", email=email
        )
        self.save(session)
        return session

    monkeypatch.setattr(SessionManager, "login_browser", fake_login)
    result = runner.invoke(app(), ["auth", "login", "--email", "a@b"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "authenticated": True,
        "user_id": "u",
        "email": "a@b",
        "expires_at": None,
    }
    assert "secret" not in result.stdout


@pytest.mark.parametrize("live", [False, True])
def test_doctor_checks_local_state_and_never_refreshes_in_m1(runner, monkeypatch, live):
    SessionManager().save(Session(access_token="secret", refresh_token="refresh", user_id="u1"))

    async def forbidden(self):
        raise AssertionError("the scaffold must not refresh")

    monkeypatch.setattr(SessionManager, "valid_session", forbidden)
    result = runner.invoke(app(), ["doctor", *(["--live"] if live else [])])
    assert result.exit_code == (1 if live else 0)
    data = json.loads(result.stdout)
    assert data["live_request_performed"] is False
    assert data["session"] is True
    assert data["session_expired"] is False
    assert data["session_refreshable"] is True
    assert data["credential_file_mode"] == "0o600"
    assert data["config_dir_mode"] == "0o700"
    assert data["playwright_importable"] is True
    assert data["chromium_executable_available"] is False
    assert data["delete_backend"] is None
    if live:
        assert data["live_read_ok"] is False


def test_doctor_missing_session_is_local_json(runner):
    result = runner.invoke(app(), ["doctor"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["session"] is False and data["credential_file_present"] is False
    assert data["live_request_performed"] is False


@pytest.mark.parametrize(
    "command",
    [["auth", "login"], ["auth", "status"], ["doctor"], ["serve", "--log-level", "bad-secret"]],
)
def test_cli_failures_do_not_render_exception_secret_or_traceback(runner, monkeypatch, command):
    def fail(*args, **kwargs):
        raise RuntimeError("private-secret-title-and-token")

    monkeypatch.setattr(SessionManager, "login_browser", fail)
    monkeypatch.setattr(SessionManager, "status", fail)
    monkeypatch.setattr("hevy_mcp.cli.SettingsStore.load", fail)
    result = runner.invoke(app(), command)
    assert result.exit_code == 1
    output = result.stdout + result.stderr
    assert "private-secret" not in output and "bad-secret" not in output
    assert "Traceback" not in output


def _delete_args(backend="v2", title="Disposable Delete Test nonce"):
    return [
        "test-delete-backend",
        "--backend",
        backend,
        "--workout-id",
        "w1",
        "--title",
        title,
        "--started-at",
        "2026-01-01T00:00:00Z",
    ]


@pytest.mark.parametrize(
    "backend,title", [("other", "Disposable Delete Test nonce"), ("v2", "Real workout")]
)
def test_delete_tester_validates_before_construction(runner, monkeypatch, backend, title):
    def forbidden(backend):
        raise AssertionError("constructed before validation")

    monkeypatch.setattr("hevy_mcp.cli.make_delete_orchestrator", forbidden)
    result = runner.invoke(app(), _delete_args(backend, title), input="y\n")
    assert result.exit_code == 2


def test_delete_tester_confirmation_precedes_construction(runner, monkeypatch):
    def forbidden(backend):
        raise AssertionError("constructed before confirmation")

    monkeypatch.setattr("hevy_mcp.cli.make_delete_orchestrator", forbidden)
    result = runner.invoke(app(), _delete_args(), input="n\n")
    assert result.exit_code == 1
    assert "Candidate: w1" in result.stdout
    assert not SessionManager().root.exists()


@pytest.mark.parametrize("verified", [False, True])
def test_tester_uses_one_candidate_and_persists_only_verified_result(runner, monkeypatch, verified):
    from hevy_mcp.config import SettingsStore

    class FakeResult:
        verified_absent = verified

        def model_dump(self, *, mode):
            assert mode == "json"
            return {
                "workout_id": "w1",
                "snapshot_path": "/fake/snapshot.json",
                "backend": "v2",
                "verified_absent": verified,
            }

    class FakeTester:
        async def delete(self, workout_id, title, started_at):
            assert (workout_id, title, started_at) == (
                "w1",
                "Disposable Delete Test nonce",
                "2026-01-01T00:00:00Z",
            )
            return FakeResult()

    candidates = []

    def build(backend):
        candidates.append(backend)
        return FakeTester()

    monkeypatch.setattr("hevy_mcp.cli.make_delete_orchestrator", build)
    result = runner.invoke(app(), _delete_args(), input="y\n")
    assert result.exit_code == (0 if verified else 1)
    assert candidates == ["v2"]
    assert SettingsStore().load().workout_delete_backend == ("v2" if verified else None)


def test_delete_tester_missing_session_fails_safely_after_confirmation(runner):
    result = runner.invoke(app(), _delete_args(), input="y\n")
    assert result.exit_code == 1
    assert "Not authenticated" in result.stderr
    assert not SessionManager().path.exists()


def test_guarded_installed_console_and_module_entry(child_env, tmp_path):
    import sys
    from pathlib import Path

    bootstrap = Path(__file__).with_name("child_bootstrap.py")
    for mode, target in [
        ("module", "hevy_mcp.cli"),
        ("path", str(Path(sys.executable).parent / "hevy-mcp")),
    ]:
        result = subprocess.run(
            [sys.executable, str(bootstrap), mode, target, "serve"],
            env=child_env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert all(
            json.loads(line)["event"] == "external_log" for line in result.stderr.splitlines()
        )


def test_fresh_auth_command_sanitizes_library_traceback(child_command, child_env):
    code = """
import logging
import sys
from hevy_mcp.auth import SessionManager
from hevy_mcp.cli import app

def fake_login(self, email):
    try:
        raise RuntimeError('private-library-exception')
    except RuntimeError:
        logging.getLogger('hevy_unofficial').exception('private-library-message')
        raise
SessionManager.login_browser = fake_login
sys.argv = ['hevy-mcp', 'auth', 'login']
app()
"""
    result = subprocess.run(
        child_command(code), env=child_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    assert "private-library" not in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
