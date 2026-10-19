from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol, cast

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

from pydantic import BaseModel

from .config import atomic_write, config_dir, secure_dir
from .errors import AuthenticationError


class Session(BaseModel):
    access_token: str
    refresh_token: str
    user_id: str | None = None
    email: str | None = None
    expires_at: datetime | None = None

    def expired(self) -> bool:
        return bool(self.expires_at and self.expires_at.astimezone(UTC) <= datetime.now(UTC))


class Refresher(Protocol):
    def refresh(self, session: Session) -> Session: ...


class UnofficialRefresher:
    def refresh(self, session: Session) -> Session:
        from hevy_unofficial import HevyClient

        try:
            c = HevyClient(
                access_token=session.access_token,
                refresh_token=session.refresh_token,
                user_id=session.user_id,
            )
            try:
                c.auth.refresh()
                tokens = c.tokens
            finally:
                c.close()
            if not tokens:
                raise AuthenticationError("refresh returned no tokens")
            return Session(
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                user_id=tokens.user_id,
                expires_at=cast(datetime | None, tokens.expires_at),
            )
        except Exception as exc:
            raise AuthenticationError("Hevy token refresh failed; run auth login") from exc


class SessionManager:
    _refresh_locks: ClassVar[dict[Path, asyncio.Lock]] = {}

    def __init__(self, root: Path | None = None, refresher: Refresher | None = None):
        self.root = root or config_dir()
        self.path = self.root / "credentials.json"
        self.lock_path = self.root / "credentials.json.lock"
        self.refresher = refresher or UnofficialRefresher()
        lock_key = self.path.expanduser().resolve()
        self._refresh_lock = self._refresh_locks.setdefault(lock_key, asyncio.Lock())

    def save(self, s: Session) -> None:
        atomic_write(self.path, s.model_dump_json(indent=2) + "\n")

    def load(self) -> Session:
        if not self.path.exists():
            raise AuthenticationError("Not authenticated; run hevy-mcp auth login")
        try:
            return Session.model_validate_json(self.path.read_text())
        except Exception as exc:
            raise AuthenticationError("Invalid local session; login again") from exc

    async def valid_session(self) -> Session:
        s = self.load()
        if s.expired():
            return await self.refresh(expected_access_token=s.access_token)
        return s

    async def refresh(self, *, expected_access_token: str | None = None) -> Session:
        """Rotate credentials once, coalescing concurrent refresh attempts."""
        async with self._refresh_lock:
            return await asyncio.to_thread(self._refresh_under_process_lock, expected_access_token)

    def _refresh_under_process_lock(self, expected_access_token: str | None) -> Session:
        with self._process_refresh_lock():
            current = self.load()
            if expected_access_token is not None and current.access_token != expected_access_token:
                return current
            refreshed = self.refresher.refresh(current)
            refreshed.email = refreshed.email or current.email
            refreshed.user_id = refreshed.user_id or current.user_id
            self.save(refreshed)
            return refreshed

    @contextmanager
    def _process_refresh_lock(self) -> Iterator[None]:
        """Serialize refreshes across processes where POSIX flock is available."""
        if fcntl is None:  # pragma: no cover - documented non-POSIX fallback
            yield
            return
        secure_dir(self.root)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def status(self) -> dict[str, Any]:
        s = self.load()
        return {
            "authenticated": True,
            "user_id": s.user_id,
            "email": s.email,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "refreshable": bool(s.refresh_token),
            "expired": s.expired(),
        }

    def logout(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def login_browser(self, email: str | None = None) -> Session:
        from hevy_unofficial import login_via_browser

        try:
            tokens, actual = login_via_browser(email=email, save=False)
        except Exception as exc:
            raise AuthenticationError(
                "Browser login failed; run `uv run playwright install chromium` and retry"
            ) from exc
        s = Session(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            user_id=tokens.user_id,
            email=actual,
            expires_at=cast(datetime | None, tokens.expires_at),
        )
        self.save(s)
        return s
