"""Explicit success variants; presentation never changes historical domain models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field, TypeAdapter

from .analytics import SetStatistics
from .catalog import TemplateSearchResult
from .models import DeletionResult, ExerciseData, SetData
from .reads import Checkpoint
from .results import Failure, PartialMutationFailure, StrictView


class WorkoutFull(StrictView):
    id: str
    title: str
    started_at: datetime
    ended_at: datetime | None
    notes: str | None
    exercises: list[ExerciseData]


class WorkoutRaw(WorkoutFull):
    raw: dict[str, Any]


class WorkoutCompact(StrictView):
    id: str
    title: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    exercise_count: int


class WorkoutCompactRaw(WorkoutCompact):
    raw: dict[str, Any]


type WorkoutView = WorkoutRaw | WorkoutFull | WorkoutCompactRaw | WorkoutCompact


class RoutineFull(StrictView):
    id: str
    title: str
    folder_id: int | str | None
    notes: str | None
    exercises: list[ExerciseData]
    index: int
    program_id: str | None
    coach_force_rpe_enabled: bool


class RoutineRaw(RoutineFull):
    raw: dict[str, Any]


class RoutineCompact(StrictView):
    id: str
    title: str
    folder_id: int | str | None
    index: int
    program_id: str | None
    coach_force_rpe_enabled: bool
    exercise_count: int


class RoutineCompactRaw(RoutineCompact):
    raw: dict[str, Any]


type RoutineView = RoutineRaw | RoutineFull | RoutineCompactRaw | RoutineCompact


class HistoryFull(StrictView):
    workout_id: str
    exercise_template_id: str
    started_at: datetime
    sets: list[SetData]


class HistoryRaw(HistoryFull):
    raw: dict[str, Any]


class HistoryCompact(StrictView):
    workout_id: str
    exercise_template_id: str
    started_at: datetime
    set_count: int


class HistoryCompactRaw(HistoryCompact):
    raw: dict[str, Any]


class HistoryStats(HistoryFull):
    stats: SetStatistics


class HistoryRawStats(HistoryRaw):
    stats: SetStatistics


class HistoryCompactStats(HistoryCompact):
    stats: SetStatistics


class HistoryCompactRawStats(HistoryCompactRaw):
    stats: SetStatistics


type HistoryView = (
    HistoryRaw
    | HistoryFull
    | HistoryCompactRaw
    | HistoryCompact
    | HistoryStats
    | HistoryRawStats
    | HistoryCompactStats
    | HistoryCompactRawStats
)


class ArrayResult[T](StrictView):
    result: list[T]


class ScanEnvelope[T](Checkpoint):
    items: list[T]


class ExerciseStatsScope(StrictView):
    kind: Literal["returned_history"]
    since: str | None
    until: str | None
    entries: int = Field(ge=0)
    sets: int = Field(ge=0)
    oldest_started_at: str | None
    newest_started_at: str | None
    started_from_origin: bool
    source_exhausted: bool
    all_accessible_history_included: bool
    upstream_history_complete: None


class StatsScope(ExerciseStatsScope):
    records_are_all_time: Literal[False]


class ExerciseStatsResult(Checkpoint):
    exercise_template_id: str
    stats: SetStatistics
    scope: ExerciseStatsScope
    records_are_all_time: Literal[False]


class ExerciseStatsRaw(ExerciseStatsResult):
    history: list[HistoryRaw]


class HistoryEnvelope(ScanEnvelope[HistoryView]):
    stats: SetStatistics
    scope: StatsScope


class RoutineEnvelope(StrictView):
    items: list[RoutineView]
    returned_count: int
    source_exhausted: Literal[True]
    window_applied: Literal["none"]


class Page(StrictView):
    items: list[WorkoutView]
    page: int = Field(ge=1)
    page_count: int = Field(ge=1)


class ScanPage(ScanEnvelope[WorkoutView]):
    page: int = Field(ge=1)
    page_count: int = Field(ge=1)


class RoutineDeleted(StrictView):
    deleted: Literal[True]
    routine_id: str


class DeletionSuccess(DeletionResult):
    model_config = ConfigDict(extra="forbid")


OUTPUTS: dict[str, Any] = {
    "search_exercise_templates": TemplateSearchResult | Failure,
    "get_exercise_stats": ExerciseStatsResult | ExerciseStatsRaw | Failure,
    "list_workouts": Page | ScanPage | Failure,
    "get_workout": WorkoutView | Failure,
    "get_recent_workouts": ArrayResult[WorkoutView] | ScanEnvelope[WorkoutView] | Failure,
    "get_exercise_history": ArrayResult[HistoryView]
    | ScanEnvelope[HistoryView]
    | HistoryEnvelope
    | Failure,
    "list_routines": ArrayResult[RoutineView] | RoutineEnvelope | Failure,
    "get_routine": RoutineView | Failure,
    "create_routine": RoutineRaw | Failure | PartialMutationFailure,
    "update_routine": RoutineRaw | Failure | PartialMutationFailure,
    "delete_routine": RoutineDeleted | Failure | PartialMutationFailure,
    "delete_workout": DeletionSuccess | Failure | PartialMutationFailure,
}
CONTRACTS: dict[str, TypeAdapter[Any]] = {
    name: TypeAdapter(model) for name, model in OUTPUTS.items()
}


def output_schema(name: str) -> dict[str, Any]:
    return {"type": "object", **CONTRACTS[name].json_schema(mode="serialization")}


def validate_output(name: str, value: Any) -> None:
    CONTRACTS[name].validate_python({"result": value} if isinstance(value, list) else value)
