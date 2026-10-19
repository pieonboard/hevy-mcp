import pytest
from hevy_unofficial.exceptions import HevyAPIError

from hevy_mcp.auth import Session
from hevy_mcp.delete_client import SessionDeleteClient
from hevy_mcp.errors import NotFoundError


class Sessions:
    async def valid_session(self):
        return Session(access_token="a", refresh_token="r")


class Workouts:
    def __init__(self):
        self.ids = []

    def get(self, workout_id):
        self.ids.append(workout_id)
        return {
            "id": workout_id,
            "title": "T",
            "start_time": "2026-01-01T00:00:00Z",
            "exercises": [],
        }


class Client:
    def __init__(self):
        self.workouts = Workouts()
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_deletion_preflight_uses_upstream_singular_get_and_closes() -> None:
    client = Client()
    result = await SessionDeleteClient(
        Sessions(), client_factory=lambda session: client
    ).get_workout("w1")
    assert result.id == "w1"
    assert client.workouts.ids == ["w1"]
    assert client.closed


@pytest.mark.asyncio
async def test_deletion_preflight_refreshes_one_401_and_closes_both_clients() -> None:
    class RefreshingSessions:
        def __init__(self):
            self.session = Session(access_token="old", refresh_token="r")
            self.refreshes = 0

        async def valid_session(self):
            return self.session

        async def refresh(self, *, expected_access_token):
            assert expected_access_token == "old"
            self.refreshes += 1
            self.session = Session(access_token="new", refresh_token="new-r")
            return self.session

    made = []

    class RefreshClient(Client):
        def __init__(self, token):
            super().__init__()
            self.token = token

        class TokenWorkouts(Workouts):
            def __init__(self, token):
                super().__init__()
                self.token = token

            def get(self, workout_id):
                if self.token == "old":
                    raise HevyAPIError("expired", status_code=401, method="GET", path="/workout/w1")
                return super().get(workout_id)

        @property
        def workouts(self):
            return self._workouts

        @workouts.setter
        def workouts(self, value):
            self._workouts = self.TokenWorkouts(self.token) if hasattr(self, "token") else value

    def factory(session):
        client = RefreshClient(session.access_token)
        client.workouts = client.TokenWorkouts(session.access_token)
        made.append(client)
        return client

    sessions = RefreshingSessions()
    workout = await SessionDeleteClient(sessions, client_factory=factory).get_workout("w1")
    assert workout.id == "w1"
    assert sessions.refreshes == 1
    assert len(made) == 2 and all(client.closed for client in made)


@pytest.mark.asyncio
async def test_deletion_preflight_maps_404_and_closes() -> None:
    client = Client()

    def missing(workout_id):
        raise HevyAPIError("gone", status_code=404, method="GET", path=f"/workout/{workout_id}")

    client.workouts.get = missing
    with pytest.raises(NotFoundError):
        await SessionDeleteClient(Sessions(), client_factory=lambda session: client).get_workout(
            "w1"
        )
    assert client.closed


@pytest.mark.asyncio
async def test_internal_delete_uses_exact_candidate_headers(monkeypatch) -> None:
    captured = []

    class Raw:
        async def request(self, method, path, **kwargs):
            captured.append((method, path, kwargs))

        async def close(self):
            pass

    client = SessionDeleteClient(Sessions())

    async def raw_client():
        return Raw()

    monkeypatch.setattr(client, "_client", raw_client)
    await client.delete("/workout/w1")
    assert captured == [
        (
            "DELETE",
            "/workout/w1",
            {
                "headers": {
                    "x-api-key": "klean_hevy_mobile",
                    "hevy-app-version": "2.5.6",
                    "hevy-app-build": "1819922",
                    "hevy-platform": "android 36",
                }
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/arbitrary/w1",
        "/v3/workouts/w1",
        "/v1/workout/w1",
        "/v1/workouts/w1/extra",
        "https://example.test/v1/workouts/w1",
    ],
)
async def test_delete_rejects_paths_outside_exact_candidate_set(path) -> None:
    with pytest.raises(ValueError, match="candidate"):
        await SessionDeleteClient(Sessions()).delete(path)
