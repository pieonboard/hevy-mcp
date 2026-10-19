"""Pure, scoped statistics over normalized history; no IO or inferred bodyweight."""

import math
from datetime import datetime
from typing import Literal

from pydantic import Field

from .models import HistoryEntry
from .results import StrictView


class RecordSet(StrictView):
    workout_id: str
    exercise_template_id: str
    started_at: datetime
    set_id: str | None
    set_index: int = Field(ge=0)
    weight_kg: float | None = Field(ge=0, allow_inf_nan=False)
    reps: int | None = Field(ge=0)
    e1rm_kg: float | None = Field(ge=0, allow_inf_nan=False)


class RepZones(StrictView):
    one_to_five: int = Field(default=0, ge=0)
    six_to_twelve: int = Field(default=0, ge=0)
    thirteen_plus: int = Field(default=0, ge=0)
    zero: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)


class SetStatistics(StrictView):
    algorithm_version: Literal["hevy-local-v1"] = "hevy-local-v1"
    set_count: int = Field(default=0, ge=0)
    working_set_count: int = Field(default=0, ge=0)
    eligible_strength_sets: int = Field(default=0, ge=0)
    excluded_warmup_sets: int = Field(default=0, ge=0)
    invalid_or_missing_load_sets: int = Field(default=0, ge=0)
    total_volume_kg: float = Field(default=0, ge=0, allow_inf_nan=False)
    best_e1rm: RecordSet | None = None
    heaviest_set: RecordSet | None = None
    max_reps_set: RecordSet | None = None
    rep_zones: RepZones = Field(default_factory=RepZones)
    warnings: list[str] = Field(default_factory=list)


def statistics(entries: list[HistoryEntry]) -> SetStatistics:
    result = SetStatistics()
    candidates: list[RecordSet] = []
    warnings = {"estimated_not_measured", "external_load_only"}
    volume = 0.0
    for entry in entries:
        for index, item in enumerate(entry.sets):
            result.set_count += 1
            if item.type == "warmup":
                result.excluded_warmup_sets += 1
                continue
            result.working_set_count += 1
            load = item.weight_kg
            if load is not None and (not math.isfinite(load) or load < 0):
                load = None
            reps = item.reps
            if load is None or reps is None:
                result.invalid_or_missing_load_sets += 1
                warnings.add("missing_load")
            else:
                contribution = load * reps
                if math.isfinite(contribution) and math.isfinite(volume + contribution):
                    volume += contribution
                else:
                    warnings.add("nonfinite_volume_excluded")
            zone = (
                "unknown"
                if reps is None
                else "zero"
                if reps == 0
                else "one_to_five"
                if reps <= 5
                else "six_to_twelve"
                if reps <= 12
                else "thirteen_plus"
            )
            setattr(result.rep_zones, zone, getattr(result.rep_zones, zone) + 1)
            estimate = None
            if load is not None and load > 0 and reps is not None and 1 <= reps <= 12:
                estimate = load if reps == 1 else load * (1 + reps / 30)
                if math.isfinite(estimate):
                    result.eligible_strength_sets += 1
                else:
                    estimate = None
            if reps is not None and reps > 12:
                warnings.add("high_rep_estimate_excluded")
            candidates.append(
                RecordSet(
                    workout_id=entry.workout_id,
                    exercise_template_id=entry.exercise_template_id,
                    started_at=entry.started_at,
                    set_id=item.id,
                    set_index=index,
                    weight_kg=load,
                    reps=reps,
                    e1rm_kg=estimate,
                )
            )

    def select(kind: str) -> RecordSet | None:
        eligible = [
            x
            for x in candidates
            if (
                x.e1rm_kg is not None
                if kind == "e1rm"
                else x.weight_kg is not None
                and x.weight_kg > 0
                and x.reps is not None
                and x.reps >= 1
                if kind == "weight"
                else x.reps is not None and x.reps >= 1
            )
        ]
        if not eligible:
            return None

        def rank(x: RecordSet) -> tuple[float | int, float | int, datetime]:
            metric = x.e1rm_kg if kind == "e1rm" else x.weight_kg if kind == "weight" else x.reps
            secondary = x.reps if kind == "weight" else x.weight_kg if kind == "reps" else 0
            return (
                metric or 0,
                secondary if secondary is not None else -1,
                x.started_at,
            )

        record = max(
            sorted(eligible, key=lambda x: (x.workout_id, x.set_index)), key=rank
        ).model_copy()
        if record.weight_kg is not None:
            record.weight_kg = round(record.weight_kg, 2)
        if record.e1rm_kg is not None:
            record.e1rm_kg = round(record.e1rm_kg, 2)
        return record

    result.total_volume_kg = round(volume, 2)
    result.best_e1rm = select("e1rm")
    result.heaviest_set = select("weight")
    result.max_reps_set = select("reps")
    result.warnings = sorted(warnings)
    return result
