import time

import httpx
import pytest
from fakes import workout
from test_tool_contracts import decoded

from hevy_mcp import errors
from hevy_mcp.auth import Session, SessionManager
from hevy_mcp.models import RoutineInput
from hevy_mcp.server import create_server
from hevy_mcp.service import HevyService, PersistentUnofficialAdapter
from hevy_mcp.upgrade_client import UpgradeClient

BASE = "https://api.hevyapp.com"


@pytest.mark.parametrize("kind", ["workouts", "recent", "history"])
async def test_upgrade_reads_real_sdk_raw_offset_timeout_no_dates_and_closes(respx_mock, kind):
    sessions = SessionManager()
    sessions.save(Session(access_token="fake", refresh_token="fake-refresh"))
    clients = []

    def factory(session):
        client = PersistentUnofficialAdapter._client(session)
        assert client.config.auto_refresh is False
        clients.append(client)
        return client

    adapter = PersistentUnofficialAdapter(sessions, factory)
    account = respx_mock.get(BASE + "/user/account").respond(200, json={"username": "fake-user"})
    path = "/user_exercise_history_paged" if kind == "history" else "/user_workouts_paged"
    route = respx_mock.get(BASE + path).respond(200, json=[workout(0)])
    result = await UpgradeClient(adapter).batch(
        kind, "t1" if kind == "history" else None, 7, 3, time.monotonic() + 2
    )
    assert result == [workout(0)]
    request = route.calls.last.request
    assert request.url.params["offset"] == "7" and request.url.params["limit"] == "3"
    assert "since" not in request.url.params and "until" not in request.url.params
    assert 0 < request.extensions["timeout"]["read"] <= 2
    assert account.call_count == (0 if kind == "history" else 1)
    assert len(clients) == 1 and clients[0]._http.is_closed


@pytest.mark.parametrize("failure", [401, 429, 503, "timeout", "malformed"])
async def test_upgrade_retry_policy_and_client_cleanup_no_outer_retry(
    respx_mock, monkeypatch, failure
):
    sessions = SessionManager()
    sessions.save(Session(access_token="fake", refresh_token="fake-refresh"))
    refreshes, sleeps, clients = [], [], []

    async def refresh(*, expected_access_token):
        refreshes.append(expected_access_token)
        return Session(access_token="new-fake", refresh_token="fake-refresh")

    monkeypatch.setattr(sessions, "refresh", refresh)

    async def sleep(delay):
        sleeps.append(delay)

    def factory(session):
        client = PersistentUnofficialAdapter._client(session)
        clients.append(client)
        return client

    adapter = PersistentUnofficialAdapter(sessions, factory, sleep)
    route = respx_mock.get(BASE + "/user_exercise_history_paged")
    if failure == "timeout":
        route.mock(side_effect=httpx.ReadTimeout("private body"))
        expected = errors.TransportError
    elif failure == "malformed":
        route.respond(200, json={"history": "broken"})
        # The scanner validates containers, not the raw read transport.
        expected = None
    else:
        route.respond(failure, json={})
        expected = {
            401: errors.AuthenticationError,
            429: errors.RateLimitError,
            503: errors.RetryableUpstreamError,
        }[failure]
    if expected:
        with pytest.raises(expected):
            await UpgradeClient(adapter).batch("history", "t1", 0, 5, time.monotonic() + 5)
    else:
        assert await UpgradeClient(adapter).batch("history", "t1", 0, 5, time.monotonic() + 5) == {
            "history": "broken"
        }
    count = 2 if failure == 401 else 1 if failure == "malformed" else 3
    assert route.call_count == count and len(clients) == count
    assert all(x._http.is_closed for x in clients)
    assert refreshes == (["fake"] if failure == 401 else [])
    assert sleeps == ([] if failure in (401, "malformed") else [0.1, 0.2])


async def test_observed_actual_worker_mutation_readback_partial_keeps_known_id(respx_mock):
    sessions = SessionManager()
    sessions.save(Session(access_token="fake", refresh_token="fake-refresh"))
    clients = []

    def factory(session):
        client = PersistentUnofficialAdapter._client(session)
        clients.append(client)
        return client

    create = respx_mock.post(BASE + "/routine").respond(200, json={"routineId": "known"})
    get = respx_mock.get(BASE + "/routine/known").respond(404, json={})
    result = await create_server(
        lambda: HevyService(PersistentUnofficialAdapter(sessions, factory))
    ).call_tool("create_routine", {"routine": RoutineInput(title="R", exercises=[]).model_dump()})
    value = decoded(result)
    assert result.is_error and value["error"]["code"] == "NOT_FOUND"
    assert value["outcome"]["request_state"] == "acknowledged"
    assert (
        value["outcome"]["resource_id"] == "known" and value["outcome"]["phase"] == "verification"
    )
    assert value["outcome"]["mutation_attempts"] == 1 and create.call_count == get.call_count == 1
    assert all(x._http.is_closed for x in clients)


async def test_pre_session_failure_does_not_claim_a_wire_attempt(monkeypatch):
    sessions = SessionManager()

    async def invalid():
        raise errors.AuthenticationError("private session text")

    monkeypatch.setattr(sessions, "valid_session", invalid)

    def forbidden(session):
        pytest.fail("client constructed despite session failure")

    result = await create_server(
        lambda: HevyService(PersistentUnofficialAdapter(sessions, forbidden))
    ).call_tool(
        "update_routine", {"routine_id": "known", "routine": {"title": "R", "exercises": []}}
    )
    value = decoded(result)
    assert value["outcome"]["phase"] == "session"
    assert (
        value["outcome"]["request_state"] == "not_attempted"
        and value["outcome"]["mutation_attempts"] == 0
    )


async def test_scan_row_limit_dedup_and_no_legacy_aggregation_on_production_path(respx_mock):
    sessions = SessionManager()
    sessions.save(Session(access_token="fake", refresh_token="fake-refresh"))
    respx_mock.get(BASE + "/user/account").respond(200, json={"username": "fake-user"})

    def page(request):
        offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
        return httpx.Response(
            200, json={"workouts": [workout(i) for i in range(offset, offset + limit)]}
        )

    route = respx_mock.get(BASE + "/user_workouts_paged").mock(side_effect=page)
    server = create_server(lambda: HevyService(PersistentUnofficialAdapter(sessions)))
    result = await server.call_tool(
        "list_workouts", {"since": "2026-01-08", "scan_limit": 10, "include_meta": True}
    )
    value = decoded(result)
    assert not result.is_error, value
    assert value["next_offset"] == 10 and value["returned_count"] == 3
    assert [
        (int(x.request.url.params["offset"]), int(x.request.url.params["limit"]))
        for x in route.calls
    ] == [(0, 5), (5, 5)]
