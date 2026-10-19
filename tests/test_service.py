from uuid import UUID

import pytest

from hevy_mcp.errors import (
    EndpointChangedError,
    IdentityMismatchError,
    NotFoundError,
    SchemaError,
)
from hevy_mcp.service import HevyService


class Adapter:
    def __init__(self):
        self.calls = []
        self.routine_title = "R"
        self.routine_deleted = False

    async def call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name == "update_routine":
            self.routine_title = args[1]["title"]
        if name == "delete_routine":
            self.routine_deleted = True
        if name == "get_routine" and self.routine_deleted:
            raise NotFoundError("gone")
        data = {
            "list_workouts": {
                "workouts": [
                    {
                        "id": "w1",
                        "title": "A",
                        "start_time": "2026-01-01T00:00:00Z",
                        "exercises": [],
                    }
                ],
                "page": 1,
                "page_count": 1,
            },
            "get_workout": {
                "id": "w1",
                "title": "A",
                "start_time": "2026-01-01T00:00:00Z",
                "exercises": [],
            },
            "exercise_history": {
                "history": [
                    {
                        "workout_id": "w1",
                        "exercise_template_id": "t1",
                        "started_at": "2026-01-01T00:00:00Z",
                        "sets": [],
                    }
                ]
            },
            "list_routines": {
                "updated": [{"id": "r1", "title": "R", "exercises": []}],
                "deleted": [],
                "isMore": False,
            },
            "get_routine": {"id": "r1", "title": self.routine_title, "exercises": []},
            "create_routine": {"id": "r1", "title": "R", "exercises": []},
            "update_routine": {"id": "r1", "title": "R2", "exercises": []},
            "delete_routine": None,
        }[name]
        return data


@pytest.mark.asyncio
async def test_workout_and_history_reads_are_normalized() -> None:
    s = HevyService(Adapter())
    assert (await s.list_workouts()).items[0].exercise_count == 0
    assert (await s.get_workout("w1")).id == "w1"
    assert len(await s.get_recent_workouts(1)) == 1
    assert (await s.get_exercise_history("t1"))[0].exercise_template_id == "t1"


@pytest.mark.asyncio
async def test_exercise_history_accepts_live_workout_rows() -> None:
    class LiveHistory(Adapter):
        async def call(self, name, *args, **kwargs):
            return [
                {
                    "id": "w-live",
                    "name": "Live workout",
                    "start_time": 1_767_225_600,
                    "exercises": [{"sets": [{"weight_kg": 80, "reps": 5}]}],
                }
            ]

    entries = await HevyService(LiveHistory()).get_exercise_history("template-live")

    assert entries[0].workout_id == "w-live"
    assert entries[0].exercise_template_id == "template-live"
    assert entries[0].sets[0].weight_kg == 80


@pytest.mark.asyncio
async def test_list_workouts_accepts_compact_summaries_without_exercises() -> None:
    class Compact(Adapter):
        async def call(self, name, *args, **kwargs):
            return {
                "workouts": [
                    {
                        "id": "w1",
                        "title": "A",
                        "start_time": "2026-01-01T00:00:00Z",
                        "exercise_count": 3,
                    }
                ],
                "page": 1,
                "page_count": 1,
            }

    summary = (await HevyService(Compact()).list_workouts()).items[0]
    assert summary.id == "w1"
    assert summary.exercise_count == 3


@pytest.mark.asyncio
async def test_list_workouts_accepts_live_name_and_unix_timestamps() -> None:
    class Live(Adapter):
        async def call(self, name, *args, **kwargs):
            return {
                "workouts": [
                    {
                        "id": "w-live",
                        "name": "Live workout",
                        "start_time": 1_767_225_600,
                        "end_time": 1_767_229_200,
                        "exercises": [],
                    }
                ]
            }

    summary = (await HevyService(Live()).list_workouts()).items[0]

    assert summary.title == "Live workout"
    assert summary.duration_seconds == 3600


@pytest.mark.asyncio
async def test_create_routine_sends_private_app_payload() -> None:
    adapter = Adapter()
    service = HevyService(adapter)

    await service.create_routine(
        {
            "title": "Private DTO",
            "exercises": [
                {
                    "exercise_template_id": "t1",
                    "sets": [
                        {"type": "warmup", "weight_kg": 20, "reps": 10},
                        {"type": "normal", "weight_kg": 60, "reps": 8},
                    ],
                }
            ],
        }
    )

    create_call = next(call for call in adapter.calls if call[0] == "create_routine")
    body = create_call[1][0]
    assert body["index"] == -1
    assert body["folder_id"] is None
    assert body["notes"] is None
    assert body["program_id"] is None
    assert body["clientId"] == body["_unsyncedObjectId"]
    UUID(body["clientId"])
    assert body["exercises"] == [
        {
            "exercise_template_id": "t1",
            "notes": "",
            "rest_seconds": 0,
            "sets": [
                {"index": 0, "indicator": "warmup", "weight_kg": 20.0, "reps": 10},
                {"index": 1, "indicator": "normal", "weight_kg": 60.0, "reps": 8},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_create_routine_reads_back_private_routine_id_response() -> None:
    class PrivateResponseAdapter(Adapter):
        async def call(self, name, *args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "create_routine":
                return {"routineId": "r-private"}
            if name == "get_routine":
                assert args == ("r-private",)
                return {"id": "r-private", "title": "R", "exercises": []}
            raise AssertionError(name)

    service = HevyService(PrivateResponseAdapter())

    created = await service.create_routine({"title": "R", "exercises": []})

    assert created.id == "r-private"


@pytest.mark.asyncio
@pytest.mark.parametrize("routine_id", [None, "", "   ", 123])
async def test_create_routine_rejects_invalid_private_routine_id(routine_id) -> None:
    class InvalidResponseAdapter(Adapter):
        async def call(self, name, *args, **kwargs):
            if name == "create_routine":
                return {"routineId": routine_id}
            raise AssertionError("read-back must not run for an invalid routineId")

    with pytest.raises(SchemaError, match="routineId"):
        await HevyService(InvalidResponseAdapter()).create_routine({"title": "R", "exercises": []})


@pytest.mark.asyncio
async def test_update_routine_sends_private_set_payload_without_create_ids() -> None:
    adapter = Adapter()
    service = HevyService(adapter)

    await service.update_routine(
        "r1",
        {
            "title": "R2",
            "index": 3,
            "exercises": [
                {
                    "exercise_template_id": "t1",
                    "rest_seconds": 90,
                    "sets": [{"type": "failure", "weight_kg": 50, "reps": 6}],
                }
            ],
        },
    )

    update_call = next(call for call in adapter.calls if call[0] == "update_routine")
    body = update_call[1][1]
    assert body["index"] == 3
    assert "clientId" not in body
    assert "_unsyncedObjectId" not in body
    assert body["exercises"][0]["sets"] == [
        {"index": 0, "indicator": "failure", "weight_kg": 50.0, "reps": 6}
    ]


@pytest.mark.asyncio
async def test_routine_crud() -> None:
    s = HevyService(Adapter())
    payload = {"title": "R", "exercises": []}
    assert (await s.list_routines())[0].id == "r1"
    assert (await s.get_routine("r1")).id == "r1"
    assert (await s.create_routine(payload)).id == "r1"
    assert (await s.update_routine("r1", {"title": "R2", "exercises": []})).title == "R2"
    assert await s.delete_routine("r1") == {"deleted": True, "routine_id": "r1"}

    names = [call[0] for call in s.adapter.calls]
    assert names[-6:] == [
        "create_routine",
        "get_routine",
        "update_routine",
        "get_routine",
        "delete_routine",
        "get_routine",
    ]


@pytest.mark.asyncio
async def test_routine_sync_rejects_incomplete_more_envelope() -> None:
    class More(Adapter):
        async def call(self, name, *args, **kwargs):
            return {"updated": [], "deleted": [], "isMore": True}

    with pytest.raises(Exception, match="isMore"):
        await HevyService(More()).list_routines()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"updated": [], "isMore": False},
        {"updated": [], "deleted": []},
        {"deleted": [], "isMore": False},
    ],
)
async def test_routine_sync_requires_every_envelope_key(response) -> None:
    class Missing(Adapter):
        async def call(self, name, *args, **kwargs):
            return response

    with pytest.raises(SchemaError, match="updated/deleted/isMore"):
        await HevyService(Missing()).list_routines()


@pytest.mark.asyncio
@pytest.mark.parametrize("template,page,size", [("", 1, 20), ("t", 0, 20), ("t", 1, 101)])
async def test_exercise_history_validates_identifier_and_pagination(template, page, size) -> None:
    with pytest.raises(ValueError):
        await HevyService(Adapter()).get_exercise_history(template, page, size)


@pytest.mark.asyncio
async def test_date_filters_are_iso_and_ordered() -> None:
    service = HevyService(Adapter())
    with pytest.raises(ValueError):
        await service.list_workouts(since="not-a-date")
    with pytest.raises(ValueError):
        await service.get_exercise_history("t", since="2026-02-01", until="2026-01-01")


def test_recent_limit_is_bounded() -> None:
    with pytest.raises(ValueError):
        HevyService(Adapter()).validate_recent_limit(101)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "response"),
    [
        ("list_workouts", {"workouts": None}),
        ("get_recent_workouts", {"data": {"id": "w1"}}),
        ("get_exercise_history", {"history": "not-a-list"}),
    ],
)
async def test_read_list_containers_fail_closed_as_schema_errors(method, response) -> None:
    class Malformed(Adapter):
        async def call(self, name, *args, **kwargs):
            return response

    service = HevyService(Malformed())
    with pytest.raises(SchemaError):
        if method == "list_workouts":
            await service.list_workouts()
        elif method == "get_recent_workouts":
            await service.get_recent_workouts()
        else:
            await service.get_exercise_history("t1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"page": None, "page_count": 1},
        {"page": "not-a-number", "page_count": 1},
        {"page": 0, "page_count": 1},
        {"page": 1, "page_count": -1},
        {"page": True, "page_count": 1},
    ],
)
async def test_invalid_workout_pagination_metadata_is_schema_error(metadata) -> None:
    response = {
        "workouts": [],
        **metadata,
    }

    class InvalidPagination(Adapter):
        async def call(self, name, *args, **kwargs):
            return response

    with pytest.raises(SchemaError, match="pagination"):
        await HevyService(InvalidPagination()).list_workouts()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        {
            "workout_id": None,
            "exercise_template_id": "t1",
            "started_at": "2026-01-01T00:00:00Z",
            "sets": [],
        },
        {
            "workout_id": "w1",
            "exercise_template_id": None,
            "started_at": "2026-01-01T00:00:00Z",
            "sets": [],
        },
        {"workout_id": "w1", "exercise_template_id": "t1", "sets": []},
    ],
)
async def test_history_rejects_null_ids_and_missing_timestamp(entry) -> None:
    class InvalidHistory(Adapter):
        async def call(self, name, *args, **kwargs):
            return {"history": [entry]}

    with pytest.raises(SchemaError, match="history schema changed"):
        await HevyService(InvalidHistory()).get_exercise_history("t1")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_routine_mutation_readback_rejects_wrong_identity(operation) -> None:
    class WrongIdentity(Adapter):
        async def call(self, name, *args, **kwargs):
            if name == "create_routine":
                return {"id": "expected", "title": "R", "exercises": []}
            if name == "get_routine":
                return {"id": "different", "title": "R", "exercises": []}
            return await super().call(name, *args, **kwargs)

    service = HevyService(WrongIdentity())
    with pytest.raises(IdentityMismatchError):
        if operation == "create":
            await service.create_routine({"title": "R", "exercises": []})
        else:
            await service.update_routine("expected", {"title": "R", "exercises": []})


@pytest.mark.asyncio
async def test_create_routine_response_without_id_is_schema_error() -> None:
    class MissingId(Adapter):
        async def call(self, name, *args, **kwargs):
            if name == "create_routine":
                return {"title": "R", "exercises": []}
            return await super().call(name, *args, **kwargs)

    with pytest.raises(SchemaError, match="routine ID missing"):
        await HevyService(MissingId()).create_routine({"title": "R", "exercises": []})


@pytest.mark.asyncio
async def test_delete_routine_still_present_is_explicit_failure() -> None:
    class StillPresent(Adapter):
        async def call(self, name, *args, **kwargs):
            if name == "delete_routine":
                return None
            if name == "get_routine":
                return {"id": "r1", "title": "R", "exercises": []}
            return await super().call(name, *args, **kwargs)

    with pytest.raises(EndpointChangedError, match="still finds"):
        await HevyService(StillPresent()).delete_routine("r1")
