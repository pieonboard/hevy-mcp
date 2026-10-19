from pathlib import Path

import pytest

from hevy_mcp.deletion import DeletionOrchestrator, SnapshotStore, backend_for
from hevy_mcp.errors import (
    AuthenticationError,
    BackendDriftError,
    EndpointChangedError,
    IdentityMismatchError,
    MutationIndeterminateError,
    NotFoundError,
    SchemaError,
    SnapshotError,
    TransportError,
)
from hevy_mcp.models import parse_workout

RAW = {
    "id": "w1",
    "title": "Push",
    "start_time": "2026-08-31T10:00:00Z",
    "exercises": [
        {"exercise_template_id": "t", "sets": [{"type": "normal", "reps": 5, "weight_kg": 80}]}
    ],
}


class Client:
    def __init__(self):
        self.deletes = []
        self.gets = 0

    async def get_workout(self, wid):
        self.gets += 1
        if self.gets > 1:
            raise NotFoundError("gone")
        return parse_workout(RAW)

    async def delete(self, path):
        self.deletes.append(path)


class BrokenStore:
    def save(self, *a, **k):
        raise OSError("disk")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title,start", [("Wrong", "2026-08-31T10:00:00Z"), ("Push", "2026-08-31T10:00:01Z")]
)
async def test_identity_mismatch_sends_zero_delete(tmp_path, title, start) -> None:
    c = Client()
    o = DeletionOrchestrator(c, SnapshotStore(tmp_path), "v2")
    with pytest.raises(IdentityMismatchError):
        await o.delete("w1", title, start)
    assert c.deletes == []


@pytest.mark.asyncio
async def test_snapshot_failure_sends_zero_delete(tmp_path) -> None:
    c = Client()
    o = DeletionOrchestrator(c, BrokenStore(), "v1")
    with pytest.raises(SnapshotError):
        await o.delete("w1", "Push", "2026-08-31T10:00:00Z")
    assert c.deletes == []


@pytest.mark.asyncio
async def test_success_snapshots_deletes_configured_backend_and_verifies(tmp_path) -> None:
    c = Client()
    result = await DeletionOrchestrator(c, SnapshotStore(tmp_path), "v2").delete(
        "w1", "Push", "2026-08-31T10:00:00+00:00"
    )
    assert c.deletes == ["/v2/workouts/w1"] and result.verified_absent
    p = Path(result.snapshot_path)
    assert p.exists() and p.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert p.name.endswith("_w1.json")


@pytest.mark.asyncio
async def test_backend_failure_is_not_fallback(tmp_path) -> None:
    class Bad(Client):
        async def delete(self, path):
            self.deletes.append(path)
            raise BackendDriftError("changed")

    c = Bad()
    with pytest.raises(BackendDriftError):
        await DeletionOrchestrator(c, SnapshotStore(tmp_path), "v1").delete(
            "w1", "Push", "2026-08-31T10:00:00Z"
        )
    assert c.deletes == ["/v1/workouts/w1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TransportError("timeout"),
        EndpointChangedError("5xx"),
        SchemaError("2xx response body was malformed"),
    ],
)
async def test_ambiguous_delete_verified_absent_is_success(tmp_path, failure) -> None:
    class Committed(Client):
        async def delete(self, path):
            self.deletes.append(path)
            raise failure

    client = Committed()
    result = await DeletionOrchestrator(client, SnapshotStore(tmp_path), "internal").delete(
        "w1", "Push", "2026-08-31T10:00:00Z"
    )
    assert result.verified_absent is True
    assert client.deletes == ["/workout/w1"]


@pytest.mark.asyncio
async def test_ambiguous_delete_still_present_is_backend_failure_without_fallback(tmp_path) -> None:
    class Present(Client):
        async def get_workout(self, wid):
            self.gets += 1
            return parse_workout(RAW)

        async def delete(self, path):
            self.deletes.append(path)
            raise TransportError("timeout")

    client = Present()
    with pytest.raises(BackendDriftError, match=r"still exists.*no fallback"):
        await DeletionOrchestrator(client, SnapshotStore(tmp_path), "v1").delete(
            "w1", "Push", "2026-08-31T10:00:00Z"
        )
    assert client.deletes == ["/v1/workouts/w1"]


@pytest.mark.asyncio
async def test_delete_readback_inconclusive_is_indeterminate_without_replay(tmp_path) -> None:
    class Inconclusive(Client):
        async def get_workout(self, wid):
            self.gets += 1
            if self.gets == 1:
                return parse_workout(RAW)
            raise AuthenticationError("read-back failed")

        async def delete(self, path):
            self.deletes.append(path)
            raise TransportError("timeout")

    client = Inconclusive()
    with pytest.raises(MutationIndeterminateError, match="read-back inconclusive"):
        await DeletionOrchestrator(client, SnapshotStore(tmp_path), "v2").delete(
            "w1", "Push", "2026-08-31T10:00:00Z"
        )
    assert client.deletes == ["/v2/workouts/w1"]


@pytest.mark.asyncio
async def test_malformed_delete_response_still_present_is_not_replayed(tmp_path) -> None:
    class Present(Client):
        async def get_workout(self, wid):
            self.gets += 1
            return parse_workout(RAW)

        async def delete(self, path):
            self.deletes.append(path)
            raise SchemaError("2xx response body was malformed")

    client = Present()
    with pytest.raises(BackendDriftError, match=r"still exists.*no fallback"):
        await DeletionOrchestrator(client, SnapshotStore(tmp_path), "internal").delete(
            "w1", "Push", "2026-08-31T10:00:00Z"
        )
    assert client.deletes == ["/workout/w1"]
    assert client.gets == 2


@pytest.mark.asyncio
async def test_malformed_delete_response_inconclusive_readback_is_not_replayed(tmp_path) -> None:
    class Inconclusive(Client):
        async def get_workout(self, wid):
            self.gets += 1
            if self.gets == 1:
                return parse_workout(RAW)
            raise AuthenticationError("read-back failed")

        async def delete(self, path):
            self.deletes.append(path)
            raise SchemaError("2xx response body was malformed")

    client = Inconclusive()
    with pytest.raises(MutationIndeterminateError, match="read-back inconclusive"):
        await DeletionOrchestrator(client, SnapshotStore(tmp_path), "v1").delete(
            "w1", "Push", "2026-08-31T10:00:00Z"
        )
    assert client.deletes == ["/v1/workouts/w1"]
    assert client.gets == 2


@pytest.mark.asyncio
async def test_delete_returned_but_workout_still_present_is_backend_failure(tmp_path) -> None:
    class Present(Client):
        async def get_workout(self, wid):
            self.gets += 1
            return parse_workout(RAW)

    with pytest.raises(BackendDriftError, match="still exists"):
        await DeletionOrchestrator(Present(), SnapshotStore(tmp_path), "internal").delete(
            "w1", "Push", "2026-08-31T10:00:00Z"
        )


@pytest.mark.asyncio
async def test_parent_directory_fsync_failure_happens_before_delete(tmp_path, monkeypatch) -> None:
    real_fsync = __import__("os").fsync
    calls = 0

    def fail_second_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr("hevy_mcp.config.os.fsync", fail_second_fsync)
    client = Client()
    with pytest.raises(SnapshotError, match="directory fsync failed"):
        await DeletionOrchestrator(client, SnapshotStore(tmp_path), "internal").delete(
            "w1", "Push", "2026-08-31T10:00:00Z"
        )
    assert client.deletes == []
    assert list(tmp_path.glob(".*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("workout_id", ["../account", "a/b", "", "x?admin=true"])
async def test_unsafe_workout_id_fails_before_get_or_snapshot(tmp_path, workout_id) -> None:
    c = Client()
    with pytest.raises(ValueError, match="unsafe workout ID"):
        await DeletionOrchestrator(c, SnapshotStore(tmp_path), "internal").delete(
            workout_id, "Push", "2026-08-31T10:00:00Z"
        )
    assert c.gets == 0 and c.deletes == [] and list(tmp_path.iterdir()) == []


def test_internal_delete_candidate_is_singular() -> None:
    assert backend_for("internal").path("w1") == "/workout/w1"


def test_snapshot_publication_is_no_overwrite_under_concurrency(tmp_path) -> None:
    import concurrent.futures
    from unittest.mock import patch

    store = SnapshotStore(tmp_path)
    workout = parse_workout(RAW)
    with patch("hevy_mcp.deletion.datetime") as clock:
        clock.now.return_value = __import__("datetime").datetime(
            2026, 8, 31, 10, tzinfo=__import__("datetime").UTC
        )
        clock.side_effect = lambda *a, **k: __import__("datetime").datetime(*a, **k)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(store.save, workout, "internal") for _ in range(8)]
    successes = [f.result() for f in futures if f.exception() is None]
    assert len(successes) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert successes[0].resolve().parent == tmp_path.resolve()
