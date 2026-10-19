import copy

import jsonschema
import pytest
from fakes import FakeService, workout
from test_tool_contracts import decoded

from hevy_mcp.analytics import statistics
from hevy_mcp.server import create_server


@pytest.mark.parametrize("raw", [False, True])
async def test_b5_uses_exact_a3_reducer_and_retains_source_records(raw):
    fake = FakeService()
    server = create_server(lambda: fake)
    result = await server.call_tool(
        "get_exercise_stats", {"exercise_template_id": "t1", "include_raw": raw}
    )
    data = decoded(result)
    expected = await fake.service.get_exercise_history("t1")
    assert data["stats"] == statistics(expected).model_dump(mode="json")
    assert data["stats"]["total_volume_kg"] == 3600
    assert data["stats"]["best_e1rm"]["e1rm_kg"] == 70
    assert data["stats"]["heaviest_set"]["set_id"] == "s1"
    assert data["stats"]["max_reps_set"]["workout_id"] == "w11"
    assert data["scope"]["entries"] == data["returned_count"] == 12
    assert data["scope"]["sets"] == 12
    assert data["scope"]["all_accessible_history_included"] is True
    assert data["scope"]["upstream_history_complete"] is None
    assert data["records_are_all_time"] is False
    assert [x[3] for x in fake.batches] == [5, 5, 5]
    assert not [x for x in fake.adapter.calls if x[0] != "exercise_history"]
    assert ("history" in data) is raw
    if raw:
        assert data["history"] == [x.model_dump(mode="json") for x in expected]
    tool = next(t for t in await server.list_tools() if t.name == "get_exercise_stats")
    jsonschema.validate(data, tool.output_schema)
    assert result.meta["hevy"]["returned_count"] == 12


async def test_b5_limit1000_is_still_bounded_and_continuation_is_separate_aggregate():
    fake = FakeService([workout(i) for i in range(250)])
    server = create_server(lambda: fake)
    args = {"exercise_template_id": "t1", "limit": 1000, "include_raw": True}
    first = decoded(await server.call_tool("get_exercise_stats", args))
    assert first["scanned_this_call"] == first["returned_count"] == 200
    assert first["truncated"] and first["has_more"] and first["next_offset"] == 200
    assert first["next_page"] is None
    assert first["scope"]["started_from_origin"] is True
    assert first["scope"]["all_accessible_history_included"] is False
    assert len(fake.batches) == 40 and all(x[3] == 5 for x in fake.batches)
    second = decoded(
        await server.call_tool("get_exercise_stats", {**args, "cursor": first["next_cursor"]})
    )
    assert second["returned_count"] == 50 and second["total_seen"] == 250
    assert second["source_exhausted"] and not second["has_more"]
    assert second["scope"]["started_from_origin"] is False
    assert second["scope"]["all_accessible_history_included"] is False
    assert second["stats"]["total_volume_kg"] == 15000
    assert second["history"][0]["workout_id"] == "w200"
    assert not first["records_are_all_time"] and not second["records_are_all_time"]
    # No implicit recursive continuation, no metric-specific upstream calls.
    assert len(fake.batches) == 51 and fake.adapter.calls == []


async def test_b5_inclusive_dates_filter_before_stats_and_full_batch_is_not_exhaustion():
    fake = FakeService()
    server = create_server(lambda: fake)
    data = decoded(
        await server.call_tool(
            "get_exercise_stats",
            {
                "exercise_template_id": "t1",
                "since": "2026-01-02",
                "until": "2026-01-02T01:00:00+01:00",
                "limit": 1,
                "include_raw": True,
            },
        )
    )
    assert data["returned_count"] == 1
    assert data["history"][0]["workout_id"] == "w1"
    assert data["window_applied"] == "client"
    assert data["since"] == data["until"] == "2026-01-02T00:00:00Z"
    assert data["has_more"] and data["next_offset"] == 2 and not data["source_exhausted"]
    assert not data["scope"]["all_accessible_history_included"]
    assert not data["records_are_all_time"]
    assert all(len(x) == 4 for x in fake.batches)  # No dates passed upstream.


@pytest.mark.parametrize("rows,options", [([], {}), ([workout(0)], {"since": "2030-01-01"})])
async def test_b5_empty_scope_keeps_honest_completeness_and_zero_metrics(rows, options):
    fake = FakeService(rows)
    data = decoded(
        await create_server(lambda: fake).call_tool(
            "get_exercise_stats", {"exercise_template_id": "t1", **options}
        )
    )
    assert data["returned_count"] == data["stats"]["set_count"] == 0
    assert (
        data["stats"]["best_e1rm"]
        is data["stats"]["heaviest_set"]
        is data["stats"]["max_reps_set"]
        is None
    )
    assert data["stats"]["total_volume_kg"] == 0
    assert data["scope"]["oldest_started_at"] is None
    assert data["source_exhausted"] and not data["records_are_all_time"]


async def test_b5_scan_cap_and_dedup_preserve_raw_source_observations():
    rows = [workout(0), workout(0), workout(1), workout(2)]
    rows[0]["exercises"][0]["sets"] += [
        {"type": "warmup", "weight_kg": 100, "reps": 5},
        {"type": "dropset", "weight_kg": 20, "reps": 15},
        {"reps": 0},
    ]
    original = copy.deepcopy(rows)
    fake = FakeService(rows)
    data = decoded(
        await create_server(lambda: fake).call_tool(
            "get_exercise_stats",
            {"exercise_template_id": "t1", "scan_limit": 3, "include_raw": True},
        )
    )
    assert data["scanned_this_call"] == 3 and data["returned_count"] == 2
    assert data["next_offset"] == 3 and data["truncated"]
    assert data["stats"]["excluded_warmup_sets"] == 1
    assert data["stats"]["rep_zones"] == {
        "one_to_five": 2,
        "six_to_twelve": 0,
        "thirteen_plus": 1,
        "zero": 1,
        "unknown": 0,
    }
    assert data["history"][0]["raw"] == original[0]
    assert fake.adapter.rows == original


@pytest.mark.parametrize(
    "options",
    [
        {"exercise_template_id": " "},
        {"limit": 0},
        {"limit": 1001},
        {"scan_limit": 0},
        {"scan_limit": 201},
        {"since": "bad"},
        {"since": "2026-02-01", "until": "2026-01-01"},
        {"cursor": "invalid!"},
    ],
)
async def test_b5_invalid_input_before_io(options):
    fake = FakeService()
    result = await create_server(lambda: fake).call_tool(
        "get_exercise_stats", {"exercise_template_id": "t1", **options}
    )
    assert result.is_error and decoded(result)["error"]["code"] == "INVALID_ARGUMENT"
    assert not fake.batches and not fake.adapter.calls


async def test_b5_cursor_cannot_change_kind_size_window_or_template():
    fake = FakeService()
    server = create_server(lambda: fake)
    first = decoded(
        await server.call_tool(
            "get_exercise_stats", {"exercise_template_id": "t1", "limit": 6, "scan_limit": 3}
        )
    )
    for change in ({"limit": 7}, {"exercise_template_id": "t2"}, {"since": "2026-01-02"}):
        before = len(fake.batches)
        result = await server.call_tool(
            "get_exercise_stats",
            {"exercise_template_id": "t1", "limit": 6, "cursor": first["next_cursor"], **change},
        )
        assert result.is_error and decoded(result)["error"]["code"] == "INVALID_ARGUMENT"
        assert len(fake.batches) == before
    result = await server.call_tool(
        "get_exercise_history",
        {"exercise_template_id": "t1", "page_size": 6, "cursor": first["next_cursor"]},
    )
    assert result.is_error and decoded(result)["error"]["code"] == "INVALID_ARGUMENT"


async def test_b5_malformed_source_is_error_never_complete_empty_success():
    fake = FakeService([{"id": "w0", "start_time": "bad", "sets": []}])
    result = await create_server(lambda: fake).call_tool(
        "get_exercise_stats", {"exercise_template_id": "t1"}
    )
    assert result.is_error and decoded(result)["error"]["code"] == "SCHEMA_ERROR"


async def test_both_profiles_exact_inventory_and_additive_schemas():
    parity = {t.name: t for t in await create_server(lambda: None, profile="parity").list_tools()}
    release = {t.name: t for t in await create_server(lambda: None).list_tools()}
    assert len(parity) == 10 and set(release) == set(parity) | {
        "search_exercise_templates",
        "get_exercise_stats",
    }
    for name, tool in parity.items():
        assert tool.parameters == release[name].parameters
        assert tool.output_schema == release[name].output_schema
        assert tool.annotations == release[name].annotations
    for name in set(release) - set(parity):
        tool = release[name]
        assert tool.parameters["additionalProperties"] is False
        assert tool.annotations.model_dump(by_alias=True)["readOnlyHint"] is True
        assert tool.annotations.destructive_hint is False
        assert tool.output_schema["type"] == "object"
    props = release["get_exercise_stats"].parameters["properties"]
    assert props["limit"]["default"] == 100 and props["limit"]["maximum"] == 1000
    assert props["scan_limit"]["maximum"] == 200
    assert not set(release) & {
        "create_workout",
        "update_workout",
        "list_routine_folders",
        "create_routine_folder",
    }


async def test_local_search_bypasses_busy_account_lane_while_stats_uses_it():
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    fake = FakeService([])

    async def blocking_batch(*args):
        entered.set()
        await release.wait()
        return []

    fake.read_batch = blocking_batch
    server = create_server(lambda: fake)
    pending = asyncio.create_task(
        server.call_tool("get_exercise_stats", {"exercise_template_id": "t1"})
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        result = await asyncio.wait_for(
            server.call_tool("search_exercise_templates", {"limit": 1}), 1
        )
        assert not result.is_error
    finally:
        release.set()
        assert not (await pending).is_error
