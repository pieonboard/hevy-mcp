"""Fixed 5-row history batches returned live, taught by the live smoke.

The live history endpoint ignores `limit` and always returns a batch of five
rows while honoring row-index offsets (live probe, 2026-10-17). The scanner
consumes only the
requested slice and lets the remaining rows re-appear on the next fetch, so
paging never skips a tail. The workouts kind keeps a strict over-return guard
because that endpoint honors the requested limit.
"""

from fakes import FakeService, workout
from test_tool_contracts import decoded

from hevy_mcp.server import create_server


class FixedBatchHistoryService(FakeService):
    """Live-like: history batches always return five rows regardless of limit."""

    async def read_batch(self, kind, exercise_id, offset, limit, deadline):
        self.batches.append((kind, exercise_id, offset, limit))
        assert 1 <= limit <= 5
        rows = self.adapter.rows[offset : offset + 5]
        return rows if kind == "history" else {"workouts": rows}


async def test_history_fixed_batch_over_return_is_sliced_not_an_error():
    fake = FixedBatchHistoryService([workout(i) for i in range(7)])
    result = await create_server(lambda: fake).call_tool(
        "get_exercise_history",
        {"exercise_template_id": "t1", "page_size": 1, "scan_limit": 200, "include_meta": True},
    )
    value = decoded(result)
    assert not result.is_error, value
    assert value["returned_count"] == 1
    assert value["items"][0]["workout_id"] == "w0"
    assert value["scanned_this_call"] == 1
    assert value["has_more"] and value["next_offset"] == 1
    # the wire request respects the remaining scan budget, not the batch size
    assert fake.batches == [("history", "t1", 0, 1)]


async def test_history_paging_never_skips_rows_across_fixed_batches():
    fake = FixedBatchHistoryService([workout(i) for i in range(12)])
    server = create_server(lambda: fake)
    seen, cursor = [], None
    while True:
        result = await server.call_tool(
            "get_exercise_history",
            {
                "exercise_template_id": "t1",
                "page_size": 2,
                "scan_limit": 200,
                "include_meta": True,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        value = decoded(result)
        assert not result.is_error, value
        seen.extend(x["workout_id"] for x in value["items"])
        if not value["has_more"]:
            break
        cursor = value["next_cursor"]
    assert seen == [f"w{i}" for i in range(12)]
    # every wire fetch asked for at most the remaining scan budget
    assert all(limit <= 5 for _, _, _, limit in fake.batches)


async def test_workout_over_return_guard_stays_strict():
    class OverReturnWorkouts(FixedBatchHistoryService):
        async def read_batch(self, kind, exercise_id, offset, limit, deadline):
            self.batches.append((kind, exercise_id, offset, limit))
            return {"workouts": self.adapter.rows[offset : offset + 7]}

    fake = OverReturnWorkouts([workout(i) for i in range(20)])
    result = await create_server(lambda: fake).call_tool(
        "list_workouts", {"page_size": 5, "include_meta": True}
    )
    value = decoded(result)
    assert result.is_error
    assert value["error"]["code"] == "SCHEMA_ERROR"
