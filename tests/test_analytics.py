import math

import jsonschema
import pytest
from fakes import FakeService
from test_tool_contracts import decoded

from hevy_mcp.analytics import statistics
from hevy_mcp.models import HistoryEntry
from hevy_mcp.server import create_server


def entry(sets, workout_id="w1", date="2026-01-01T00:00:00Z"):
    return HistoryEntry(
        workout_id=workout_id, exercise_template_id="t1", started_at=date, sets=sets
    )


def test_formula_warmups_dropsets_failure_and_volume():
    value = statistics(
        [
            entry(
                [
                    {"type": "warmup", "weight_kg": 200, "reps": 10},
                    {"type": "dropset", "weight_kg": 60, "reps": 5},
                    {"type": "failure", "weight_kg": 80, "reps": 1},
                    {"weight_kg": 70, "reps": 13},
                    {"weight_kg": 0, "reps": 0},
                ]
            )
        ]
    )
    assert value.set_count == 5 and value.working_set_count == 4 and value.excluded_warmup_sets == 1
    assert value.total_volume_kg == 1290 and value.eligible_strength_sets == 2
    assert value.best_e1rm.e1rm_kg == 80 and value.best_e1rm.set_index == 2
    assert value.heaviest_set.weight_kg == 80
    assert value.max_reps_set.reps == 13
    assert value.rep_zones.model_dump() == {
        "one_to_five": 2,
        "six_to_twelve": 0,
        "thirteen_plus": 1,
        "zero": 1,
        "unknown": 0,
    }
    assert "high_rep_estimate_excluded" in value.warnings


@pytest.mark.parametrize("weight", [None, -10, math.inf, -math.inf, math.nan, 0])
@pytest.mark.parametrize("reps", [None, 0, 1, 12, 13])
def test_missing_nonfinite_negative_zero_load_and_reps(weight, reps):
    value = statistics([entry([{"weight_kg": weight, "reps": reps}])])
    valid_load = weight is not None and math.isfinite(weight) and weight >= 0
    assert value.invalid_or_missing_load_sets == int(not valid_load or reps is None)
    assert value.total_volume_kg == (weight * reps if valid_load and reps is not None else 0)
    assert value.best_e1rm is None
    assert value.heaviest_set is None
    if reps and reps >= 1:
        assert value.max_reps_set.weight_kg == (weight if valid_load else None)
        assert value.max_reps_set.set_index == 0
    else:
        assert value.max_reps_set is None
    assert "NaN" not in value.model_dump_json() and "Infinity" not in value.model_dump_json()


def test_ties_use_unrounded_values_dates_ids_source_indexes_and_stable_order():
    sets = [{"weight_kg": 60, "reps": 5}, {"id": "second", "weight_kg": 60, "reps": 5}]
    rows = [
        entry(sets, "z", "2026-02-01"),
        entry(sets, "a", "2026-02-01"),
        entry(sets, "older", "2026-01-01"),
    ]
    value = statistics(rows)
    for record in (value.best_e1rm, value.heaviest_set, value.max_reps_set):
        assert record.workout_id == "a" and record.set_index == 0 and record.set_id is None
    unrounded = statistics(
        [entry([{"weight_kg": 60.001, "reps": 5}, {"weight_kg": 60.002, "reps": 5}])]
    )
    assert unrounded.best_e1rm.set_index == 1 and unrounded.best_e1rm.weight_kg == 60
    # Heaviest ties choose reps, max-reps ties choose known higher load before recency.
    v = statistics(
        [
            entry([{"weight_kg": 60, "reps": 5}], "old"),
            entry([{"weight_kg": 60, "reps": 4}], "new", "2026-02-01"),
        ]
    )
    assert v.heaviest_set.workout_id == "old"
    v = statistics(
        [entry([{"reps": 5}], "new", "2026-02-01"), entry([{"weight_kg": 60, "reps": 5}], "old")]
    )
    assert v.max_reps_set.workout_id == "old"


def test_empty_and_repeated_observations_are_honest():
    value = statistics([])
    assert value.total_volume_kg == 0 and value.best_e1rm is None and value.set_count == 0
    row = entry([{"weight_kg": 60, "reps": 5}])
    assert statistics([row, row]).total_volume_kg == 600


@pytest.mark.parametrize("format", [None, "summary", "full"])
@pytest.mark.parametrize("include_meta", [False, True])
async def test_a3_preserves_observations_no_extra_io_and_scoped_metrics(format, include_meta):
    fake = FakeService()
    server = create_server(lambda: fake)
    args = {
        "exercise_template_id": "t1",
        "page_size": 6,
        "format": format,
        "include_meta": include_meta,
    }
    plain = await server.call_tool("get_exercise_history", args)
    count = len(fake.batches) + len(fake.adapter.calls)
    result = await server.call_tool("get_exercise_history", {**args, "include_stats": True})
    assert not result.is_error, decoded(result)
    assert len(fake.batches) + len(fake.adapter.calls) == count * 2
    value = decoded(result)
    plain = decoded(plain)
    rows = value["items"] if include_meta else value
    plain_rows = plain["items"] if include_meta else plain
    for row, before in zip(rows, plain_rows, strict=True):
        assert row["stats"]["total_volume_kg"] == 300
        assert {k: v for k, v in row.items() if k != "stats"} == before
    scope = value["scope"] if include_meta else result.meta["hevy"]["stats_scope"]
    assert scope["records_are_all_time"] is False and scope["upstream_history_complete"] is None
    assert scope["entries"] == 6 and scope["sets"] == 6
    assert not scope["all_accessible_history_included"]
    if include_meta:
        assert value["stats"]["total_volume_kg"] == 1800
    schema = next(
        t.output_schema for t in await server.list_tools() if t.name == "get_exercise_history"
    )
    jsonschema.validate(result.structured_content, schema)
