import httpx
import pytest

from hevy_mcp.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    EndpointChangedError,
    NotFoundError,
    SchemaError,
    TransportError,
)
from hevy_mcp.raw_client import RawHevyClient


@pytest.mark.asyncio
async def test_get_retries_429_and_5xx_bounded() -> None:
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429 if calls == 1 else 200, json={"ok": True})

    c = RawHevyClient("token", transport=httpx.MockTransport(handler), sleep=lambda _: _noop())
    assert await c.request("GET", "/x") == {"ok": True} and calls == 2


async def _noop():
    pass


@pytest.mark.asyncio
async def test_mutation_is_never_retried() -> None:
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={})

    c = RawHevyClient("token", transport=httpx.MockTransport(handler), sleep=lambda _: _noop())
    with pytest.raises(EndpointChangedError):
        await c.request("DELETE", "/x")
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error"),
    [(401, AuthenticationError), ("timeout", TransportError)],
)
async def test_delete_auth_or_timeout_is_never_replayed(failure, error) -> None:
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(failure)

    client = RawHevyClient("token", transport=httpx.MockTransport(handler), retries=5)
    with pytest.raises(error):
        await client.request("DELETE", "/workout/w1")
    assert calls == 1


@pytest.mark.asyncio
async def test_status_and_schema_mapping() -> None:
    for status, error in [
        (401, AuthenticationError),
        (403, AuthorizationError),
        (404, NotFoundError),
        (409, ConflictError),
    ]:
        c = RawHevyClient("t", transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s)))
        with pytest.raises(error):
            await c.request("GET", "/x")
    c = RawHevyClient("t", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="no")))
    with pytest.raises(SchemaError):
        await c.request("GET", "/x")
