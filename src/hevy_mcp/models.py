from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import SchemaError

SetType = Literal["normal", "warmup", "dropset", "failure"]


def normalize_timestamp(value: str | int | float | datetime) -> datetime:
    if isinstance(value, bool):
        raise TypeError("timestamp missing or invalid")
    if isinstance(value, int | float):
        value = datetime.fromtimestamp(value, tz=UTC)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError("timestamp missing or invalid")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


class SetData(BaseModel):
    """Permissive upstream response set; unknown fields survive schema evolution."""

    model_config = ConfigDict(extra="allow")
    id: str | None = None
    type: SetType = Field("normal", validation_alias=AliasChoices("type", "indicator"))
    weight_kg: float | None = None
    reps: int | None = Field(None, ge=0)
    distance_meters: float | None = Field(None, ge=0)
    duration_seconds: int | None = Field(None, ge=0)
    rpe: float | None = Field(None, ge=0, le=10)
    notes: str | None = None


class ExerciseData(BaseModel):
    """Permissive upstream response exercise."""

    model_config = ConfigDict(extra="allow")
    id: str | None = None
    exercise_template_id: str
    title: str | None = None
    notes: str | None = None
    superset_id: int | None = None
    rest_seconds: int | None = Field(None, ge=0)
    sets: list[SetData] = Field(default_factory=list)


class RoutineSetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    type: SetType = "normal"
    weight_kg: float | None = Field(None, ge=0)
    reps: int | None = Field(None, ge=0)
    distance_meters: float | None = Field(None, ge=0)
    duration_seconds: int | None = Field(None, ge=0)
    rpe: float | None = Field(None, ge=0, le=10)
    notes: str | None = None


class RoutineExerciseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    exercise_template_id: str
    title: str | None = None
    notes: str | None = None
    superset_id: int | None = None
    rest_seconds: int | None = Field(None, ge=0)
    sets: list[RoutineSetInput] = Field(default_factory=list)

    @field_validator("exercise_template_id")
    @classmethod
    def nonempty_template_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("exercise_template_id must not be empty")
        return value


class Workout(BaseModel):
    id: str
    title: str
    started_at: datetime
    ended_at: datetime | None = None
    notes: str | None = None
    exercises: list[ExerciseData] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class WorkoutSummary(BaseModel):
    id: str
    title: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    exercise_count: int


class WorkoutPage(BaseModel):
    items: list[WorkoutSummary]
    page: int
    page_count: int


class HistoryEntry(BaseModel):
    workout_id: str
    exercise_template_id: str
    started_at: datetime
    sets: list[SetData]
    raw: dict[str, Any] = Field(default_factory=dict)


class RoutineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    folder_id: int | str | None = None
    notes: str | None = None
    exercises: list[RoutineExerciseInput]
    index: int = Field(0, ge=0)
    program_id: str | None = None
    coach_force_rpe_enabled: bool = False


class Routine(BaseModel):
    id: str
    title: str
    folder_id: int | str | None = None
    notes: str | None = None
    exercises: list[ExerciseData] = Field(default_factory=list)
    index: int = 0
    program_id: str | None = None
    coach_force_rpe_enabled: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class DeletionResult(BaseModel):
    workout_id: str
    snapshot_path: str
    backend: str
    verified_absent: bool


def _pick(d: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in d:
            return d[key]
    return None


def _complete_exercises(raw: dict[str, Any]) -> list[Any]:
    exercises = raw["exercises"]
    if not isinstance(exercises, list):
        raise TypeError("exercises is not a list")
    for exercise in exercises:
        if not isinstance(exercise, dict):
            raise TypeError("exercise is not an object")
        if "sets" not in exercise or not isinstance(exercise["sets"], list):
            raise TypeError("exercise sets missing or not a list")
    return exercises


def parse_workout(raw: dict[str, Any]) -> Workout:
    try:
        if not isinstance(raw, dict):
            raise TypeError("response is not an object")
        raw_id = _pick(raw, "id", "workout_id")
        if raw_id is None or not str(raw_id).strip():
            raise ValueError("workout ID missing")
        raw_title = _pick(raw, "title", "name")
        if raw_title is None or not str(raw_title).strip():
            raise ValueError("workout title missing")
        raw_started_at = _pick(raw, "start_time", "started_at")
        if raw_started_at is None:
            raise ValueError("workout start timestamp missing")
        return Workout(
            id=str(raw_id),
            title=str(raw_title),
            started_at=normalize_timestamp(raw_started_at),
            ended_at=normalize_timestamp(value)
            if (value := _pick(raw, "end_time", "ended_at"))
            else None,
            notes=_pick(raw, "description", "notes"),
            exercises=_complete_exercises(raw),
            raw=raw,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise SchemaError(f"Workout schema changed: {exc}") from exc


def parse_routine(raw: dict[str, Any]) -> Routine:
    try:
        if not isinstance(raw, dict):
            raise TypeError("response is not an object")
        obj = raw.get("routine", raw)
        if not isinstance(obj, dict):
            raise TypeError("routine is not an object")
        routine_id = _pick(obj, "id", "routine_id", "routineId")
        if routine_id is None or not str(routine_id).strip():
            raise ValueError("routine ID missing")
        return Routine(
            id=str(routine_id),
            title=obj["title"],
            folder_id=obj.get("folder_id"),
            notes=obj.get("notes"),
            exercises=_complete_exercises(obj),
            index=obj.get("index", 0),
            program_id=obj.get("program_id"),
            coach_force_rpe_enabled=obj.get("coach_force_rpe_enabled", False),
            raw=raw,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise SchemaError(f"Routine schema changed: {exc}") from exc
