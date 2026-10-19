"""Integration checks against mocked HTTP and the real adapter factories."""

import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from hevy_mcp.auth import Session, SessionManager
from hevy_mcp.config import SettingsStore
from hevy_mcp.delete_client import SessionDeleteClient
from hevy_mcp.deletion import DeletionOrchestrator, SnapshotStore
from hevy_mcp.errors import (
    AuthenticationError,
    EndpointChangedError,
    MutationIndeterminateError,
    RateLimitError,
    RetryableUpstreamError,
    TransportError,
)
from hevy_mcp.models import RoutineInput, parse_workout
from hevy_mcp.raw_client import RawHevyClient
from hevy_mcp.server import default_service
from hevy_mcp.service import (
    PersistentUnofficialAdapter,
    private_create_routine_payload,
    private_update_routine_payload,
)

BASE = "https://api.hevyapp.com"
RAW = {
    "id": "w1",
    "name": "Push",
    "start_time": 1767225600,
    "description": "private note",
    "upstream_extra": {"keep": True},
    "exercises": [{"exercise_template_id": "t1", "sets": [{"indicator": "warmup", "reps": 5}]}],
}


async def noop(_):
    pass


@pytest.mark.parametrize("backend", [None, "internal", "v1", "v2"])
def test_default_service_builds_shared_session_without_account_io(tmp_path, monkeypatch, backend):
    root = Path(__import__("os").environ["HEVY_CONFIG_DIR"])
    if backend:
        SettingsStore().set_delete_backend(backend)
    before = {p.name: p.read_bytes() for p in root.glob("*")}

    def forbidden(*args, **kwargs):
        pytest.fail("account IO during default_service construction")

    monkeypatch.setattr(SessionManager, "load", forbidden)
    monkeypatch.setattr(SessionManager, "login_browser", forbidden)
    monkeypatch.setattr("hevy_unofficial.HevyClient", forbidden)
    service = default_service()
    assert isinstance(service.adapter, PersistentUnofficialAdapter)
    assert service.adapter.sessions.root == root
    if backend:
        assert isinstance(service.deletion, DeletionOrchestrator)
        assert service.deletion.client.sessions is service.adapter.sessions
        assert service.deletion.backend.name == backend
        assert service.deletion.snapshots.root == root / "deleted-workouts"
    else:
        assert service.deletion is None
        assert not root.exists()
    assert {p.name: p.read_bytes() for p in root.glob("*")} == before


def test_private_dto_accepts_but_does_not_transmit_unsupported_fields():
    routine = RoutineInput.model_validate(
        {
            "title": "DTO",
            "index": 7,
            "coach_force_rpe_enabled": True,
            "exercises": [
                {
                    "id": "exercise-id",
                    "title": "display-only",
                    "exercise_template_id": "t1",
                    "superset_id": 2,
                    "sets": [
                        {"id": "set-id", "rpe": 8, "notes": "accepted", "reps": 0},
                        {"type": "failure", "weight_kg": 0, "duration_seconds": 0},
                    ],
                }
            ],
        }
    )
    create = private_create_routine_payload(routine)
    update = private_update_routine_payload(routine)
    assert str(UUID(create["clientId"])) == create["_unsyncedObjectId"]
    assert create["index"] == -1 and update["index"] == 7
    assert create == {
        **update,
        "index": -1,
        "clientId": create["clientId"],
        "_unsyncedObjectId": create["clientId"],
    }
    assert update == {
        "title": "DTO",
        "index": 7,
        "folder_id": None,
        "notes": None,
        "program_id": None,
        "exercises": [
            {
                "exercise_template_id": "t1",
                "notes": "",
                "rest_seconds": 0,
                "superset_id": 2,
                "sets": [
                    {"index": 0, "indicator": "normal", "reps": 0},
                    {"index": 1, "indicator": "failure", "weight_kg": 0, "duration_seconds": 0},
                ],
            }
        ],
    }


def test_snapshot_preserves_raw_and_normalized_after_raw_free_presentation(tmp_path):
    workout = parse_workout(RAW)
    presentation = workout.model_dump(mode="json", exclude={"raw"})
    assert "raw" not in presentation
    path = SnapshotStore(tmp_path).save(workout, "internal")
    snapshot = json.loads(path.read_text())
    assert set(snapshot) == {"snapshot_version", "created_at", "backend", "workout", "raw"}
    assert snapshot["raw"] == RAW
    assert snapshot["workout"] == workout.model_dump(mode="json")
    assert snapshot["workout"]["raw"] == RAW
    assert snapshot["workout"]["exercises"][0]["sets"][0]["type"] == "warmup"


@pytest.mark.parametrize(
    ("failure", "error"),
    [(429, RateLimitError), (503, RetryableUpstreamError), ("timeout", TransportError)],
)
async def test_sdk_read_exhausts_exactly_three_wire_attempts_and_closes(
    tmp_path, respx_mock, failure, error
):
    sessions = SessionManager(tmp_path)
    sessions.save(Session(access_token="a", refresh_token="r"))
    clients = []
    sleeps = []

    def factory(session):
        client = PersistentUnofficialAdapter._client(session)
        assert client.config.auto_refresh is False
        clients.append(client)
        return client

    async def sleep(delay):
        sleeps.append(delay)

    route = respx_mock.get(BASE + "/workout/w1")
    if failure == "timeout":
        route.mock(side_effect=httpx.ReadTimeout("synthetic"))
    else:
        route.respond(failure, json={})
    with pytest.raises(error):
        await PersistentUnofficialAdapter(sessions, factory, sleep).call("get_workout", "w1")
    assert route.call_count == 3 and respx_mock.calls.call_count == 3
    assert sleeps == [0.1, 0.2]
    assert len(clients) == 3 and all(c._http.is_closed for c in clients)


@pytest.mark.parametrize(("status", "attempts"), [(401, 2), (429, 1), (503, 1)])
async def test_singular_delete_preflight_keeps_narrow_retry_policy(respx_mock, status, attempts):
    class Sessions:
        refreshes = 0

        async def valid_session(self):
            return Session(access_token="old", refresh_token="r")

        async def refresh(self, *, expected_access_token):
            assert expected_access_token == "old"
            self.refreshes += 1
            return Session(access_token="new", refresh_token="r")

    clients = []

    def factory(session):
        client = SessionDeleteClient._upstream_client(session)
        assert client.config.auto_refresh is False
        clients.append(client)
        return client

    sessions = Sessions()
    route = respx_mock.get(BASE + "/workout/w1").respond(status, json={})
    error = {401: AuthenticationError, 429: RateLimitError, 503: RetryableUpstreamError}[status]
    with pytest.raises(error):
        await SessionDeleteClient(sessions, factory).get_workout("w1")
    assert route.call_count == attempts and respx_mock.calls.call_count == attempts
    assert sessions.refreshes == (1 if status == 401 else 0)
    assert len(clients) == attempts and all(c._http.is_closed for c in clients)


@pytest.mark.parametrize("action", ["create_routine", "update_routine", "delete_routine"])
@pytest.mark.parametrize("failure", [401, 429, 503, "timeout"])
async def test_sdk_mutations_have_one_wire_attempt_no_refresh_and_close(
    tmp_path, respx_mock, action, failure
):
    sessions = SessionManager(tmp_path)
    sessions.save(Session(access_token="a", refresh_token="r"))
    clients = []

    def factory(session):
        client = PersistentUnofficialAdapter._client(session)
        assert client.config.auto_refresh is False
        clients.append(client)
        return client

    method, path, args = {
        "create_routine": ("POST", "/routine", ({"title": "R", "exercises": []},)),
        "update_routine": ("PUT", "/routine/r1", ("r1", {"title": "R", "exercises": []})),
        "delete_routine": ("DELETE", "/routine/r1", ("r1",)),
    }[action]
    route = respx_mock.route(method=method, url__startswith=BASE + path)
    if failure == "timeout":
        route.mock(side_effect=httpx.ReadTimeout("synthetic"))
    else:
        route.respond(failure, json={})
    with pytest.raises(MutationIndeterminateError):
        await PersistentUnofficialAdapter(sessions, factory, noop).call(action, *args)
    assert route.call_count == 1 and respx_mock.calls.call_count == 1
    assert len(clients) == 1 and clients[0]._http.is_closed


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_raw_read_exhausts_three_wire_attempts_and_closes(respx_mock, method):
    route = respx_mock.route(method=method, url=BASE + "/workout/w1").respond(503)
    client = RawHevyClient("a", sleep=noop)
    try:
        with pytest.raises(EndpointChangedError):
            await client.request(method, "/workout/w1")
        assert route.call_count == 3 and respx_mock.calls.call_count == 3
    finally:
        await client.close()
    assert client.http.is_closed
