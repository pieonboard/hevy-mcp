"""Bounded raw-offset reads, separate from legacy public-page aggregation."""

import asyncio
import time
from typing import Any

from .errors import AuthenticationError, RateLimitError, RetryableUpstreamError, TransportError
from .service import PersistentUnofficialAdapter, UnofficialAdapter, map_upstream_error


class UpgradeClient:
    def __init__(self, adapter: PersistentUnofficialAdapter):
        self.adapter = adapter

    async def batch(
        self, kind: str, exercise_id: str | None, offset: int, limit: int, deadline: float
    ) -> Any:
        session = await self.adapter.sessions.valid_session()
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError("Scanner deadline exhausted during batch")
            client = self.adapter.client_factory(session)

            def invoke(client: Any = client) -> Any:
                # Configure the actual HTTP client, whose timeout the SDK captures
                # at construction; recompute inside the worker after scheduling.
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TransportError("Scanner deadline exhausted before request")
                client._http.timeout = min(client.config.timeout, left)
                if kind == "history":
                    return client.exercises.history_paged(
                        {"exerciseTemplateId": exercise_id, "offset": offset, "limit": limit}
                    )
                username = UnofficialAdapter(client)._authenticated_username()
                # Account lookup also consumes this batch's deadline.
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TransportError("Scanner deadline exhausted after account lookup")
                client._http.timeout = min(client.config.timeout, left)
                return client.workouts.list_paged(username=username, offset=offset, limit=limit)

            try:
                try:
                    return await asyncio.to_thread(invoke)
                except TransportError:
                    raise
                except Exception as exc:
                    raise map_upstream_error(exc) from None
            except AuthenticationError:
                if attempt:
                    raise
                session = await self.adapter.sessions.refresh(
                    expected_access_token=session.access_token
                )
            except (RateLimitError, RetryableUpstreamError, TransportError):
                if attempt == 2:
                    raise
                delay = 0.1 * (2**attempt)
                if time.monotonic() + delay >= deadline:
                    raise
                await self.adapter.sleeper(delay)
            finally:
                await asyncio.to_thread(client.close)
        raise AssertionError("unreachable")
