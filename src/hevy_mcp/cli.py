from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import click
import typer
from typer.core import TyperGroup

from .auth import SessionManager
from .config import SettingsStore, config_dir
from .errors import AuthenticationError, ConfigurationError
from .logging import configure_logging
from .runtime import DeleteTester, UnavailableOperation


class SafeGroup(TyperGroup):
    """Catch execution failures before Click/Typer can render their exception chain."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            configure_logging()
            return super().invoke(ctx)
        except (click.ClickException, click.exceptions.Exit, click.Abort):
            raise
        except Exception as exc:
            if isinstance(exc, UnavailableOperation):
                message = "This operation is not implemented in this build."
            elif isinstance(exc, AuthenticationError):
                message = "Not authenticated or invalid local session; run hevy-mcp auth login."
            elif isinstance(exc, ConfigurationError):
                message = "Invalid local configuration; inspect config.yaml."
            else:
                message = "Command failed; inspect local configuration and sanitized logs."
            typer.echo(message, err=True)
            raise typer.Exit(1) from None


app = typer.Typer(
    cls=SafeGroup,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Unofficial local Hevy MCP server",
)
auth_app = typer.Typer(help="Manage local Hevy browser-session credentials")
app.add_typer(auth_app, name="auth")


def make_delete_orchestrator(backend: str) -> DeleteTester:
    from .delete_client import SessionDeleteClient
    from .deletion import DeletionOrchestrator, SnapshotStore

    return DeletionOrchestrator(
        SessionDeleteClient(SessionManager()),
        SnapshotStore(config_dir() / "deleted-workouts"),
        backend,
    )


def _print(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@auth_app.command("login")
def auth_login(
    email: Annotated[str | None, typer.Option(help="Expected account email")] = None,
) -> None:
    """Open Hevy in a browser and persist the auth2.0 session cookie."""
    s = SessionManager().login_browser(email)
    _print(
        {"authenticated": True, "user_id": s.user_id, "email": s.email, "expires_at": s.expires_at}
    )


@auth_app.command("status")
def auth_status() -> None:
    _print(SessionManager().status())


@auth_app.command("logout")
def auth_logout() -> None:
    SessionManager().logout()
    _print({"authenticated": False, "local_session_removed": True, "remote_revoked": False})


@app.command()
def serve(log_level: str = "INFO") -> None:
    configure_logging(log_level)
    from .server import serve as run

    run(log_level=log_level)


@app.command()
def doctor(
    live: Annotated[
        bool,
        typer.Option("--live", help="Validate/refresh the session and perform one harmless read"),
    ] = False,
) -> None:
    root = config_dir()
    credentials = root / "credentials.json"
    checks: dict[str, Any] = {
        "config_dir": str(root),
        "config_dir_mode": oct(root.stat().st_mode & 0o777) if root.exists() else None,
        "credential_file_present": credentials.exists(),
        "credential_file_mode": oct(credentials.stat().st_mode & 0o777)
        if credentials.exists()
        else None,
        "session": False,
        "session_expired": None,
        "session_refreshable": False,
        "playwright_importable": False,
        "chromium_executable_available": False,
        "delete_backend": SettingsStore().load().workout_delete_backend,
        "live_request_performed": False,
    }
    sessions = SessionManager()
    try:
        session = sessions.load()
        checks["session"] = True
        checks["session_expired"] = session.expired()
        checks["session_refreshable"] = bool(session.refresh_token)
    except Exception:
        pass
    try:
        import playwright

        del playwright
        checks["playwright_importable"] = True
        dry_run = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        match = re.search(r"Install location:\s*(.+)", dry_run.stdout)
        checks["chromium_executable_available"] = bool(
            match and Path(match.group(1).strip()).is_dir()
        )
    except Exception:
        pass
    if live:
        checks["live_read_ok"] = False
        import asyncio

        from .server import default_service

        async def live_read() -> None:
            session = await sessions.valid_session()
            checks["user_id"] = session.user_id
            checks["email"] = session.email
            checks["live_request_performed"] = True
            await default_service().get_recent_workouts(1)
            checks["live_read_ok"] = True

        try:
            asyncio.run(live_read())
        except Exception:
            typer.echo("Live read failed; inspect session and upstream compatibility.", err=True)
    _print(checks)
    if not checks["session"] or (live and not checks.get("live_read_ok")):
        raise typer.Exit(1)


@app.command("test-delete-backend")
def test_delete_backend(
    backend: Annotated[str, typer.Option(help="Exactly one candidate: internal, v1, or v2")],
    workout_id: Annotated[str, typer.Option()],
    title: Annotated[str, typer.Option()],
    started_at: Annotated[str, typer.Option()],
) -> None:
    """Verify one backend only against a manually-created disposable workout."""
    if backend not in {"internal", "v1", "v2"}:
        raise typer.BadParameter("backend must be internal, v1, or v2")
    if not re.fullmatch(r"Disposable Delete Test .+", title):
        raise typer.BadParameter("title must be 'Disposable Delete Test <nonce>'")
    typer.echo(f"Candidate: {workout_id} | {title} | {started_at} | backend={backend}")
    if not typer.confirm("Permanently delete this disposable workout using this one backend?"):
        raise typer.Abort()
    import asyncio

    result = asyncio.run(make_delete_orchestrator(backend).delete(workout_id, title, started_at))
    if not result.verified_absent:
        raise typer.Exit(1)
    SettingsStore().set_delete_backend(backend)
    _print(result.model_dump(mode="json"))


if __name__ == "__main__":
    app()
