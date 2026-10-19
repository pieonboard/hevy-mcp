import base64
import json

import jsonschema
import pytest
from fakes import FakeService, workout
from test_tool_contracts import decoded

from hevy_mcp.reads import Reads, decode_cursor
from hevy_mcp.runtime import Runtime
from hevy_mcp.server import create_server


@pytest.mark.parametrize(
    "kind,size",
    [
        (kind, size)
        for kind in ("workouts", "history", "recent")
        for size in (1, 5, 6, 10, 12, 50, 100)
        if kind != "workouts" or size <= 50
    ],
)
async def test_consumed_offset_continuations_never_skip_tail(size, kind):
    fake = FakeService([workout(i) for i in range(117)])
    server = create_server(lambda: fake)
    name = {
        "workouts": "list_workouts",
        "history": "get_exercise_history",
        "recent": "get_recent_workouts",
    }[kind]
    base = {"limit" if kind == "recent" else "page_size": size, "include_meta": True}
    if kind == "history":
        base["exercise_template_id"] = "t1"
    seen, total, cursor = [], 0, None
    while True:
        result = await server.call_tool(name, {**base, **({"cursor": cursor} if cursor else {})})
        assert not result.is_error, decoded(result)
        value = decoded(result)
        rows = value["items"]
        seen.extend(x["workout_id"] if kind == "history" else x["id"] for x in rows)
        total += value["scanned_this_call"]
        assert value["total_seen"] == total and value["returned_count"] == len(rows)
        assert result.meta["hevy"]["next_offset"] == value["next_offset"]
        assert (
            value["consistency"] == "best_effort_offset"
            and value["page_count_authoritative"] is False
        )
        if not value["has_more"]:
            assert value["next_offset"] is None and value["source_exhausted"]
            break
        cursor = value["next_cursor"]
        assert decode_cursor(cursor).next_offset == total
        assert value["next_page"] == (
            total // size + 1 if kind != "recent" and total % size == 0 else None
        )
    assert seen == [f"w{i}" for i in range(117)]
    assert all(limit <= 5 for _, _, _, limit in fake.batches)
    assert sum(x[3] for x in fake.batches) >= 117


@pytest.mark.parametrize("count", [0, 4, 5, 10])
async def test_short_and_full_terminal_batches_are_honest(count):
    fake = FakeService([workout(i) for i in range(count)])
    result = await create_server(lambda: fake).call_tool(
        "list_workouts", {"page_size": 10, "include_meta": True}
    )
    value = decoded(result)
    assert value["has_more"] is (count == 10)
    assert value["stop_reason"] == ("output_limit" if count == 10 else "source_exhausted")


@pytest.mark.parametrize("name", ["list_workouts", "get_recent_workouts", "get_exercise_history"])
async def test_window_consumes_filtered_rows_non_aligned_and_survives_restart(name):
    fake = FakeService([workout(i) for i in range(20)])
    server = create_server(lambda: fake)
    args = {
        "since": "2026-01-08",
        "until": "2026-01-10T01:00:00+01:00",
        "scan_limit": 10,
        "include_meta": True,
    }
    if name == "get_exercise_history":
        args["exercise_template_id"] = "t1"
        args["page_size"] = 6
    elif name == "list_workouts":
        args["page_size"] = 6
    else:
        args["limit"] = 6
    first = decoded(await server.call_tool(name, args))
    assert len(first["items"]) == 3 and first["next_offset"] == 10
    assert first["next_page"] is None and first["truncated"]
    assert first["window_applied"] == "client" and first["since"] == "2026-01-08T00:00:00Z"
    assert first["until"] == "2026-01-10T00:00:00Z"
    assert first["total_seen"] == 10 and not first["scan_complete"]
    second = decoded(
        await create_server(lambda: fake).call_tool(name, {**args, "cursor": first["next_cursor"]})
    )
    assert second["items"] == [] and second["next_offset"] == 20 and second["has_more"]
    third = decoded(await server.call_tool(name, {**args, "cursor": second["next_cursor"]}))
    assert third["items"] == [] and not third["has_more"] and third["total_seen"] == 20
    assert all(len(batch) == 4 for batch in fake.batches)  # No dates on the wire seam.


async def test_filtered_empty_is_not_terminal_and_old_window_is_reachable():
    fake = FakeService([workout(i) for i in range(12)][::-1])
    server = create_server(lambda: fake)
    args = {"since": "2026-01-01", "until": "2026-01-01", "include_meta": True, "scan_limit": 5}
    first = decoded(await server.call_tool("list_workouts", args))
    assert first["items"] == [] and first["has_more"] and first["next_offset"] == 5
    for _ in range(2):
        first = decoded(
            await server.call_tool("list_workouts", {**args, "cursor": first["next_cursor"]})
        )
    assert [x["id"] for x in first["items"]] == ["w0"]
    assert first["source_exhausted"]


@pytest.mark.parametrize(
    "since,until", [("bad", None), ("2026-02-01", "2026-01-01"), ("2026-01-32", None)]
)
async def test_invalid_windows_before_io(since, until):
    fake = FakeService()
    result = await create_server(lambda: fake).call_tool(
        "list_workouts", {"since": since, "until": until}
    )
    assert result.is_error and decoded(result)["error"]["code"] == "INVALID_ARGUMENT"
    assert not fake.batches and not fake.adapter.calls


@pytest.mark.parametrize(
    "change",
    [
        "kind",
        "size",
        "exercise",
        "since",
        "page",
        "fingerprint",
        "version",
        "offset",
        "bool",
        "unknown",
        "huge",
        "invalid",
    ],
)
async def test_cursor_validation_and_scope_changes_before_io(change):
    fake = FakeService()
    server = create_server(lambda: fake)
    first = decoded(
        await server.call_tool("list_workouts", {"include_meta": True, "scan_limit": 3})
    )
    cursor = first["next_cursor"]
    raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    args = {"cursor": cursor}
    name = "list_workouts"
    if change == "kind":
        name = "get_recent_workouts"
    elif change == "size":
        args["page_size"] = 5
    elif change == "exercise":
        name = "get_exercise_history"
        args["exercise_template_id"] = "other"
    elif change == "since":
        args["since"] = "2026-01-01"
    elif change == "page":
        args["page"] = 2
    elif change == "huge":
        args["cursor"] = "a" * 2049
    elif change == "invalid":
        args["cursor"] = "bad!!"
    else:
        if change == "fingerprint":
            raw["fingerprint"] = "0" * 64
        elif change == "version":
            raw["version"] = 2
        elif change == "offset":
            raw["next_offset"] = -1
        elif change == "bool":
            raw["next_offset"] = True
        else:
            raw["extra"] = 1
        args["cursor"] = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")
    fake.batches.clear()
    result = await server.call_tool(name, args)
    assert result.is_error and decoded(result)["error"]["code"] == "INVALID_ARGUMENT"
    assert not fake.batches


async def test_display_changes_preserve_cursor_and_page_n_total_starts_zero():
    fake = FakeService()
    server = create_server(lambda: fake)
    value = decoded(
        await server.call_tool(
            "list_workouts", {"page": 2, "page_size": 6, "include_meta": True, "scan_limit": 3}
        )
    )
    assert value["total_seen"] == 3 and value["next_offset"] == 9
    result = await server.call_tool(
        "list_workouts",
        {"page_size": 6, "cursor": value["next_cursor"], "format": "full", "include_raw": True},
    )
    assert not result.is_error
    assert [x["id"] for x in decoded(result)["items"]] == ["w9", "w10", "w11"]
    assert result.meta["hevy"]["total_seen"] == 6


async def test_dedup_validates_duplicates_and_retains_source_count():
    rows = [workout(0), workout(0), workout(1)]
    fake = FakeService(rows)
    result = await create_server(lambda: fake).call_tool(
        "get_exercise_history", {"exercise_template_id": "t1", "include_meta": True}
    )
    value = decoded(result)
    assert len(value["items"]) == 2 and value["total_seen"] == 3
    fake.adapter.rows[1]["exercises"] = None
    result = await create_server(lambda: fake).call_tool(
        "get_exercise_history", {"exercise_template_id": "t1", "include_meta": True}
    )
    assert result.is_error and decoded(result)["error"]["code"] == "SCHEMA_ERROR"


@pytest.mark.parametrize("budget", ["batch", "deadline"])
async def test_explicit_batch_time_budget_stops_before_next_request(budget):
    fake = FakeService()
    runtime = Runtime(lambda: fake)
    reads = Reads(runtime)
    if budget == "batch":
        reads.max_batches = 1
    else:
        ticks = iter([0, 0, 121])
        reads.clock = lambda: next(ticks)
    items, meta = await reads.scan(
        "workouts",
        page=1,
        size=10,
        exercise_id=None,
        since=None,
        until=None,
        cursor=None,
        scan_limit=200,
    )
    assert len(items) == 5 and len(fake.batches) == 1
    assert meta["stop_reason"] == ("batch_limit" if budget == "batch" else "deadline")
    assert meta["truncated"] and meta["next_offset"] == 5


async def test_metadata_only_keeps_per_item_array_and_empty_wire_shape():
    for rows in ([], [workout(0)]):
        fake = FakeService(rows)
        server = create_server(lambda fake=fake: fake)
        result = await server.call_tool("get_recent_workouts", {"scan_limit": 2})
        assert "result" in result.structured_content
        assert len(result.content) == len(rows)
        assert result.meta["hevy"]["source_exhausted"]
        schema = next(
            x.output_schema for x in await server.list_tools() if x.name == "get_recent_workouts"
        )
        jsonschema.validate(result.structured_content, schema)


async def test_oversized_scope_and_malformed_batch_fail_closed():
    fake = FakeService()
    result = await create_server(lambda: fake).call_tool(
        "get_exercise_history", {"exercise_template_id": "x" * 2000, "include_meta": True}
    )
    assert result.is_error
    assert not fake.batches

    async def malformed(*args):
        return {"workouts": [workout(0)], "page_count": False}

    fake.read_batch = malformed
    result = await create_server(lambda: fake).call_tool("list_workouts", {"include_meta": True})
    assert result.is_error and decoded(result)["error"]["code"] == "SCHEMA_ERROR"


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
async def test_cursor_version_rejects_noninteger_before_io(version):
    fake = FakeService()
    server = create_server(lambda: fake)
    first = decoded(
        await server.call_tool("list_workouts", {"include_meta": True, "scan_limit": 2})
    )
    cursor = decode_cursor(first["next_cursor"]).model_dump()
    cursor["version"] = version
    encoded = base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode().rstrip("=")
    fake.batches.clear()
    result = await server.call_tool("list_workouts", {"cursor": encoded})
    assert result.is_error and decoded(result)["error"]["code"] == "INVALID_ARGUMENT"
    assert not fake.batches
