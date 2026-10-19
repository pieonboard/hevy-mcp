from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    EndpointChangedError,
    HevyError,
    NotFoundError,
    RateLimitError,
    SchemaError,
    TransportError,
)

log = logging.getLogger(__name__)


class RawHevyClient:
    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = "https://api.hevyapp.com",
        transport: httpx.AsyncBaseTransport | None = None,
        retries: int = 2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.retries = retries
        self.sleep = sleep
        self.http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(20),
            headers={
                "Authorization": f"Bearer {access_token}",
                "x-api-key": "shelobs_hevy_web",
                "hevy-platform": "web",
                "Accept": "application/json",
                "User-Agent": "hevy-mcp/0.1",
            },
        )

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        attempts = self.retries + 1 if method.upper() in {"GET", "HEAD"} else 1
        for attempt in range(1, attempts + 1):
            try:
                r = await self.http.request(method, path, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt < attempts:
                    await self.sleep(0)
                    continue
                raise TransportError("Hevy request timed out or failed") from exc
            if r.status_code in {429} or r.status_code >= 500:
                if attempt < attempts:
                    log.warning(
                        "retry method=%s endpoint=%s status=%s attempt=%s",
                        method,
                        path,
                        r.status_code,
                        attempt,
                    )
                    await self.sleep(0)
                    continue
            if r.status_code == 401:
                raise AuthenticationError("Hevy session rejected")
            if r.status_code == 403:
                raise AuthorizationError("Hevy request forbidden")
            if r.status_code == 404:
                raise NotFoundError(f"Hevy resource not found: {path}")
            if r.status_code == 409:
                raise ConflictError("Hevy conflict")
            if r.status_code == 429:
                raise RateLimitError("Hevy rate limit exceeded")
            if r.status_code >= 500:
                raise EndpointChangedError(f"Hevy endpoint failed ({r.status_code})")
            if r.status_code >= 400:
                raise HevyError(f"Hevy request failed ({r.status_code})")
            if r.status_code == 204:
                return None
            try:
                return r.json()
            except ValueError as exc:
                raise SchemaError("Hevy returned invalid JSON") from exc
        raise AssertionError("unreachable")

    async def close(self) -> None:
        await self.http.aclose()
