from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from .auth import Session, SessionManager
from .deletion import validate_workout_id
from .errors import AuthenticationError
from .models import parse_workout
from .raw_client import RawHevyClient
from .service import map_upstream_error


class SessionDeleteClient:
    def __init__(
        self,
        sessions: SessionManager,
        client_factory: Callable[[Session], Any] | None = None,
    ):
        self.sessions = sessions
        self.client_factory = client_factory or self._upstream_client

    @staticmethod
    def _upstream_client(session: Session) -> Any:
        from hevy_unofficial import HevyClient
        from hevy_unofficial.config import HevyConfig

        return HevyClient(
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            user_id=session.user_id,
            config=HevyConfig(auto_refresh=False),
        )

    async def _client(self) -> RawHevyClient:
        return RawHevyClient((await self.sessions.valid_session()).access_token)

    async def get_workout(self, workout_id: str) -> Any:
        validate_workout_id(workout_id)
        session = await self.sessions.valid_session()
        for attempt in range(2):
            client = self.client_factory(session)
            try:
                try:
                    raw = await asyncio.to_thread(client.workouts.get, workout_id)
                except Exception as exc:
                    mapped = map_upstream_error(exc)
                    if isinstance(mapped, AuthenticationError) and attempt == 0:
                        session = await self.sessions.refresh(
                            expected_access_token=session.access_token
                        )
                        continue
                    raise mapped from exc
                return parse_workout(raw)
            finally:
                await asyncio.to_thread(client.close)
        raise AssertionError("unreachable")

    async def delete(self, path: str) -> Any:
        match = re.fullmatch(r"/(?:workout|v[12]/workouts)/([A-Za-z0-9_-]+)", path)
        if match is None:
            raise ValueError("delete path is not an exact workout candidate")
        validate_workout_id(match.group(1))
        c = await self._client()
        try:
            headers = None
            if path.startswith("/workout/"):
                headers = {
                    "x-api-key": "klean_hevy_mobile",
                    "hevy-app-version": "2.5.6",
                    "hevy-app-build": "1819922",
                    "hevy-platform": "android 36",
                }
            return await c.request("DELETE", path, headers=headers)
        finally:
            await c.close()
