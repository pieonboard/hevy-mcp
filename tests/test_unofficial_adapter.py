from datetime import UTC, datetime, timedelta

import httpx
import pytest

from hevy_mcp.auth import Session, SessionManager
from hevy_mcp.errors import (
    AuthorizationError,
    ConflictError,
    MutationIndeterminateError,
    NotFoundError,
    RetryableUpstreamError,
)
from hevy_mcp.service import PersistentUnofficialAdapter, UnofficialAdapter, map_upstream_error


class API:
    def __init__(self):
        self.calls = []

    def list_paged(self, **kwargs):
        self.calls.append(kwargs)
        return {"workouts": []}

    def history_paged(self, params):
        self.calls.append(params)
        return {"history": []}


class Client:
    def __init__(self):
        self.workouts = API()
        self.exercises = API()

        class Users:
            def get_account(self):
                return {"user": {"username": "actual-user"}}

        self.users = Users()


@pytest.mark.asyncio
async def test_unofficial_workout_list_resolves_authenticated_username() -> None:
    class Users:
        def get_account(self):
            return {"user": {"username": "actual-user"}}

    client = Client()
    client.users = Users()

    await UnofficialAdapter(client).call("list_workouts", page=1, limit=10)

    assert client.workouts.calls == [{"username": "actual-user", "limit": 5, "offset": 0}]


@pytest.mark.asyncio
async def test_unofficial_workout_pagination_maps_page_to_offset_and_drops_date_filters() -> None:
    client = Client()
    adapter = UnofficialAdapter(client)
    await adapter.call("list_workouts", page=3, limit=10, since="2026-01-01", until=None)
    assert client.workouts.calls == [{"username": "actual-user", "limit": 5, "offset": 20}]


@pytest.mark.asyncio
async def test_unofficial_workout_list_aggregates_fixed_five_row_pages() -> None:
    class Workouts:
        def __init__(self):
            self.offsets = []

        def list_paged(self, **kwargs):
            self.offsets.append(kwargs["offset"])
            start = kwargs["offset"]
            # Hevy caps batches at 5 regardless of requested limit.
            count = min(kwargs["limit"], 5)
            return {"workouts": [{"id": str(index)} for index in range(start, start + count)]}

    class Users:
        def get_account(self):
            return {"user": {"username": "actual-user"}}

    client = Client()
    client.workouts = Workouts()
    client.users = Users()

    result = await UnofficialAdapter(client).call("list_workouts", page=1, limit=12)

    assert len(result["workouts"]) == 12
    assert client.workouts.offsets == [0, 5, 10]


@pytest.mark.asyncio
async def test_unofficial_exercise_history_uses_history_paged() -> None:
    client = Client()
    adapter = UnofficialAdapter(client)
    await adapter.call("exercise_history", "template", page=2, limit=20, since=None, until=None)
    assert client.exercises.calls == [{"exerciseTemplateId": "template", "limit": 20, "offset": 20}]


@pytest.mark.asyncio
async def test_unofficial_exercise_history_aggregates_fixed_five_row_pages() -> None:
    class Exercises:
        def __init__(self):
            self.offsets = []

        def history_paged(self, params):
            self.offsets.append(params["offset"])
            start = params["offset"]
            return [{"id": str(index)} for index in range(start, min(start + 5, 13))]

    client = Client()
    client.exercises = Exercises()

    rows = await UnofficialAdapter(client).call(
        "exercise_history", "template", page=1, limit=12, since=None, until=None
    )

    assert len(rows) == 12
    assert client.exercises.offsets == [0, 5, 10]


def test_upstream_status_mapping() -> None:
    from hevy_unofficial.exceptions import HevyAPIError

    assert isinstance(
        map_upstream_error(HevyAPIError("no", status_code=404, method="GET", path="/routine/x")),
        NotFoundError,
    )
    assert isinstance(
        map_upstream_error(HevyAPIError("no", status_code=403, method="GET", path="/routine/x")),
        AuthorizationError,
    )
    assert isinstance(
        map_upstream_error(HevyAPIError("no", status_code=409, method="GET", path="/routine/x")),
        ConflictError,
    )


@pytest.mark.asyncio
async def test_mutation_401_is_not_replayed() -> None:
    from hevy_unofficial.exceptions import HevyAPIError

    class Routines:
        calls = 0

        def create(self, body):
            self.calls += 1
            raise HevyAPIError("auth", status_code=401, method="POST", path="/routine")

    client = Client()
    client.routines = Routines()
    with pytest.raises(MutationIndeterminateError, match=r"reason=auth"):
        await UnofficialAdapter(client).call("create_routine", {"title": "x"})
    assert client.routines.calls == 1


@pytest.mark.asyncio
async def test_reactive_read_refresh_is_persisted_and_clients_close(tmp_path) -> None:
    from hevy_unofficial.exceptions import HevyAPIError

    class Refresher:
        def refresh(self, session):
            return Session(
                access_token="new",
                refresh_token="new-r",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

    sessions = SessionManager(tmp_path, refresher=Refresher())
    sessions.save(Session(access_token="old", refresh_token="old-r"))
    made = []

    class Reads:
        def __init__(self, token):
            self.token = token

        def get(self, workout_id):
            if self.token == "old":
                raise HevyAPIError("expired", status_code=401, method="GET", path="/workout/w")
            return {"id": "w", "title": "T", "start_time": "2026-01-01T00:00:00Z", "exercises": []}

    class C:
        def __init__(self, session):
            self.workouts = Reads(session.access_token)
            self.closed = False

        def close(self):
            self.closed = True

    def factory(session):
        client = C(session)
        made.append(client)
        return client

    raw = await PersistentUnofficialAdapter(sessions, factory).call("get_workout", "w")
    assert raw["id"] == "w"
    assert sessions.load().access_token == "new"
    assert len(made) == 2 and all(client.closed for client in made)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [429, 503, "timeout"])
async def test_read_transient_failure_retries_at_most_three_times(tmp_path, failure) -> None:
    from hevy_unofficial.exceptions import HevyAPIError

    sessions = SessionManager(tmp_path)
    sessions.save(Session(access_token="token", refresh_token="refresh"))
    attempts = 0
    clients = []

    class Workouts:
        def get(self, workout_id):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                if failure == "timeout":
                    raise httpx.ReadTimeout("slow")
                raise HevyAPIError(
                    "transient", status_code=failure, method="GET", path="/workout/w"
                )
            return {"id": workout_id}

    class Client:
        def __init__(self):
            self.workouts = Workouts()
            self.closed = False

        def close(self):
            self.closed = True

    def factory(session):
        client = Client()
        clients.append(client)
        return client

    async def no_sleep(delay):
        assert delay in {0.1, 0.2}

    result = await PersistentUnofficialAdapter(sessions, factory, sleeper=no_sleep).call(
        "get_workout", "w"
    )
    assert result == {"id": "w"}
    assert attempts == 3
    assert all(client.closed for client in clients)


@pytest.mark.asyncio
async def test_exhausted_read_retries_raise_and_close_every_client(tmp_path) -> None:
    from hevy_unofficial.exceptions import HevyAPIError

    sessions = SessionManager(tmp_path)
    sessions.save(Session(access_token="token", refresh_token="refresh"))
    attempts = 0
    clients = []

    class Workouts:
        def get(self, workout_id):
            nonlocal attempts
            attempts += 1
            raise HevyAPIError("down", status_code=503, method="GET", path="/workout/w")

    class Client:
        def __init__(self):
            self.workouts = Workouts()
            self.closed = False

        def close(self):
            self.closed = True

    def factory(session):
        client = Client()
        clients.append(client)
        return client

    async def no_sleep(delay):
        pass

    with pytest.raises(RetryableUpstreamError):
        await PersistentUnofficialAdapter(sessions, factory, sleeper=no_sleep).call(
            "get_workout", "w"
        )
    assert attempts == 3
    assert len(clients) == 3 and all(client.closed for client in clients)


@pytest.mark.asyncio
async def test_mutation_503_is_sent_once_and_reported_indeterminate(tmp_path) -> None:
    from hevy_unofficial.exceptions import HevyAPIError

    sessions = SessionManager(tmp_path)
    sessions.save(Session(access_token="token", refresh_token="refresh"))
    attempts = 0

    class Routines:
        def create(self, body):
            nonlocal attempts
            attempts += 1
            raise HevyAPIError("transient", status_code=503, method="POST", path="/routine")

    client = None

    class Client:
        def __init__(self):
            self.routines = Routines()
            self.closed = False

        def close(self):
            self.closed = True

    def factory(session):
        nonlocal client
        client = Client()
        return client

    with pytest.raises(MutationIndeterminateError):
        await PersistentUnofficialAdapter(sessions, factory).call("create_routine", {"title": "x"})
    assert attempts == 1
    assert client is not None and client.closed
