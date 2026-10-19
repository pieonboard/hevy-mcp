from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from hevy_mcp.errors import SchemaError
from hevy_mcp.models import RoutineInput, normalize_timestamp, parse_routine, parse_workout


def test_parse_workout_preserves_complete_sets_and_raw() -> None:
    raw = {
        "id": "w1",
        "title": "Push",
        "start_time": "2026-08-31T10:00:00Z",
        "end_time": "2026-08-31T11:00:00Z",
        "description": "note",
        "exercises": [
            {
                "id": "e1",
                "exercise_template_id": "t1",
                "title": "Press",
                "sets": [{"id": "s1", "type": "normal", "weight_kg": 80, "reps": 5}],
            }
        ],
    }
    workout = parse_workout(raw)
    assert workout.id == "w1" and workout.exercises[0].sets[0].weight_kg == 80
    assert workout.raw == raw


def test_parse_workout_accepts_live_name_and_unix_second_timestamps() -> None:
    workout = parse_workout(
        {
            "id": "w-live",
            "name": "Live workout",
            "start_time": 1_767_225_600,
            "end_time": 1_767_229_200,
            "exercises": [],
        }
    )

    assert workout.title == "Live workout"
    assert workout.started_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert workout.ended_at == datetime(2026, 1, 1, 1, tzinfo=UTC)


def test_timestamp_normalization_equates_offsets() -> None:
    assert normalize_timestamp("2026-08-31T12:00:00+02:00") == datetime(2026, 8, 31, 10, tzinfo=UTC)


def test_routine_rejects_unknown_set_type() -> None:
    with pytest.raises(ValidationError):
        RoutineInput(
            title="X", exercises=[{"exercise_template_id": "t", "sets": [{"type": "bogus"}]}]
        )


@pytest.mark.parametrize("value", ["", "   "])
def test_routine_rejects_empty_exercise_template_id(value) -> None:
    with pytest.raises(ValidationError):
        RoutineInput(title="X", exercises=[{"exercise_template_id": value, "sets": []}])


def test_routine_rejects_negative_weight_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RoutineInput(
            title="X", exercises=[{"exercise_template_id": "t", "sets": [{"weight_kg": -1}]}]
        )
    with pytest.raises(ValidationError):
        RoutineInput(
            title="X", exercises=[{"exercise_template_id": "t", "server_owned": True, "sets": []}]
        )


@pytest.mark.parametrize(
    "raw",
    [
        {"title": "X", "start_time": "2026-01-01T00:00:00Z"},
        {"id": None, "title": "X", "start_time": "2026-01-01T00:00:00Z"},
    ],
)
def test_workout_missing_id_is_schema_error(raw) -> None:
    with pytest.raises(SchemaError):
        parse_workout(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "w1", "title": None, "start_time": "2026-01-01T00:00:00Z"},
        {"id": "w1", "title": "", "start_time": "2026-01-01T00:00:00Z"},
        {"id": "w1", "title": "   ", "start_time": "2026-01-01T00:00:00Z"},
        {"id": "w1", "title": "Workout"},
        {"id": "w1", "title": "Workout", "start_time": None},
    ],
)
def test_workout_rejects_null_blank_title_and_missing_timestamp(raw) -> None:
    with pytest.raises(SchemaError, match="Workout schema changed"):
        parse_workout(raw)


def test_routine_response_normalizes_private_indicator_to_type() -> None:
    routine = parse_routine(
        {
            "id": "r",
            "title": "R",
            "exercises": [
                {
                    "exercise_template_id": "t",
                    "sets": [{"index": 0, "indicator": "warmup", "weight_kg": 20, "reps": 10}],
                }
            ],
        }
    )

    parsed_set = routine.exercises[0].sets[0]
    assert parsed_set.type == "warmup"
    assert "indicator" not in (parsed_set.model_extra or {})


def test_routine_response_preserves_unknown_nested_fields() -> None:
    routine = parse_routine(
        {
            "id": "r",
            "title": "R",
            "exercises": [
                {
                    "exercise_template_id": "t",
                    "unknown": 1,
                    "sets": [{"type": "normal", "unknown": 2}],
                }
            ],
        }
    )
    assert routine.exercises[0].model_extra == {"unknown": 1}
    assert routine.exercises[0].sets[0].model_extra == {"unknown": 2}


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "w1", "title": "W", "start_time": "2026-01-01T00:00:00Z"},
        {
            "id": "w1",
            "title": "W",
            "start_time": "2026-01-01T00:00:00Z",
            "exercises": None,
        },
        {
            "id": "w1",
            "title": "W",
            "start_time": "2026-01-01T00:00:00Z",
            "exercises": [{"exercise_template_id": "t1"}],
        },
        {
            "id": "w1",
            "title": "W",
            "start_time": "2026-01-01T00:00:00Z",
            "exercises": [{"exercise_template_id": "t1", "sets": None}],
        },
    ],
)
def test_complete_workout_rejects_absent_or_malformed_exercises_and_sets(raw) -> None:
    with pytest.raises(SchemaError, match="Workout schema changed"):
        parse_workout(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "r1", "title": "R"},
        {"id": "r1", "title": "R", "exercises": None},
        {"id": "r1", "title": "R", "exercises": [{"exercise_template_id": "t1"}]},
        {
            "id": "r1",
            "title": "R",
            "exercises": [{"exercise_template_id": "t1", "sets": "not-a-list"}],
        },
    ],
)
def test_complete_routine_rejects_absent_or_malformed_exercises_and_sets(raw) -> None:
    with pytest.raises(SchemaError, match="Routine schema changed"):
        parse_routine(raw)
