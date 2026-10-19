"""Compatibility facade, finite source scanner, projections and cursor v1."""

import base64
import hashlib
import time
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator

from .analytics import statistics
from .errors import SchemaError
from .models import (
    HistoryEntry,
    Routine,
    Workout,
    WorkoutSummary,
    normalize_timestamp,
    parse_workout,
)
from .results import StrictView
from .runtime import Runtime, current
from .service import HevyService, PersistentUnofficialAdapter
from .upgrade_client import UpgradeClient

Format = Literal["summary", "full"] | None


class Scope(StrictView):
    kind: Literal["workouts", "recent", "history", "stats"]
    exercise_id: str | None = Field(default=None, max_length=512)
    since: str | None
    until: str | None
    size: int = Field(ge=1, le=1000)


class Cursor(StrictView):
    version: Literal[1] = 1
    next_offset: int = Field(ge=0, le=2**53 - 1, strict=True)
    total_seen: int = Field(ge=0, le=2**53 - 1, strict=True)
    scope: Scope
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("cursor version must be an integer")
        return value


class Checkpoint(StrictView):
    next_offset: int | None = Field(ge=0)
    next_page: int | None = Field(ge=1)
    next_cursor: str | None
    has_more: bool
    total_seen: int = Field(ge=0)
    scanned_this_call: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    source_exhausted: bool
    scan_complete: bool
    truncated: bool
    window_applied: Literal["none", "client"]
    since: str | None
    until: str | None
    consistency: Literal["best_effort_offset"] = "best_effort_offset"
    stop_reason: Literal[
        "output_limit", "source_exhausted", "scan_limit", "batch_limit", "deadline"
    ]
    page_count_authoritative: Literal[False] = False


def fingerprint(scope: Scope) -> str:
    return hashlib.sha256(scope.model_dump_json().encode()).hexdigest()


def encode_cursor(scope: Scope, offset: int, total: int) -> str:
    data = Cursor(next_offset=offset, total_seen=total, scope=scope, fingerprint=fingerprint(scope))
    result = base64.urlsafe_b64encode(data.model_dump_json().encode()).decode().rstrip("=")
    if len(result) > 2048:
        raise ValueError("cursor scope exceeds bound")
    return result


def decode_cursor(value: str) -> Cursor:
    import re

    if not value or len(value) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid cursor")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = Cursor.model_validate_json(raw, strict=True)
        if cursor.scope.kind in {"history", "stats"}:
            if not cursor.scope.exercise_id or not cursor.scope.exercise_id.strip():
                raise ValueError("missing exercise scope")
        elif cursor.scope.exercise_id is not None:
            raise ValueError("unexpected exercise scope")
        if cursor.scope.kind == "workouts" and cursor.scope.size > 50:
            raise ValueError("invalid workout size")
        if cursor.scope.kind in {"recent", "history"} and cursor.scope.size > 100:
            raise ValueError("invalid history/recent size")
        start, end = dates(cursor.scope.since, cursor.scope.until)
        if (start, end) != (cursor.scope.since, cursor.scope.until):
            raise ValueError("noncanonical date scope")
        if fingerprint(cursor.scope) != cursor.fingerprint:
            raise ValueError("cursor fingerprint mismatch")
        return cursor
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc


def dates(since: str | None, until: str | None) -> tuple[str | None, str | None]:
    HevyService._validate_dates(since, until)
    return (
        normalize_timestamp(since).isoformat().replace("+00:00", "Z") if since else None,
        normalize_timestamp(until).isoformat().replace("+00:00", "Z") if until else None,
    )


def raw_enabled(format: Format, include_raw: bool | None, *, detail: bool, summary: bool) -> bool:
    if include_raw is not None:
        return include_raw
    if format is None:
        return not summary
    return detail and format == "full"


def project(
    item: Workout | WorkoutSummary | Routine | HistoryEntry,
    format: Format,
    include_raw: bool | None,
    *,
    detail: bool = False,
    include_stats: bool = False,
) -> dict[str, Any]:
    summary = format == "summary" or isinstance(item, WorkoutSummary)
    value = item.model_dump(mode="json")
    if summary:
        if isinstance(item, Workout):
            value = WorkoutSummary(
                id=item.id,
                title=item.title,
                started_at=item.started_at,
                ended_at=item.ended_at,
                duration_seconds=int((item.ended_at - item.started_at).total_seconds())
                if item.ended_at
                else None,
                exercise_count=len(item.exercises),
            ).model_dump(mode="json")
        elif isinstance(item, Routine):
            value = {
                key: value[key]
                for key in (
                    "id",
                    "title",
                    "folder_id",
                    "index",
                    "program_id",
                    "coach_force_rpe_enabled",
                )
            }
            value["exercise_count"] = len(item.exercises)
        elif isinstance(item, HistoryEntry):
            value = {
                key: value[key] for key in ("workout_id", "exercise_template_id", "started_at")
            }
            value["set_count"] = len(item.sets)
    if raw_enabled(format, include_raw, detail=detail, summary=summary) and hasattr(item, "raw"):
        value["raw"] = item.raw
    else:
        value.pop("raw", None)
    if include_stats and isinstance(item, HistoryEntry):
        value["stats"] = statistics([item]).model_dump(mode="json")
    return value


class RowsAdapter:
    """Reuse the unchanged historical history parser over already fetched rows."""

    def __init__(self, rows: list[Any]):
        self.rows = rows

    async def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self.rows


class Reads:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.service = runtime.service
        self.clock = time.monotonic
        self.seconds = 120.0
        self.max_batches = 40

    async def batch(
        self, kind: str, exercise_id: str | None, offset: int, limit: int, deadline: float
    ) -> Any:
        # Fake services implement this explicit raw-offset seam; production uses
        # the shared adapter's sessions/factory, never public page aggregation.
        if hasattr(self.service, "read_batch"):
            return await self.service.read_batch(kind, exercise_id, offset, limit, deadline)
        adapter = self.service.adapter
        adapter = getattr(adapter, "raw", adapter)
        if not isinstance(adapter, PersistentUnofficialAdapter):
            raise SchemaError("Raw-offset read capability missing")
        return await UpgradeClient(adapter).batch(kind, exercise_id, offset, limit, deadline)

    async def scan(
        self,
        kind: Literal["workouts", "recent", "history", "stats"],
        *,
        page: int,
        size: int,
        exercise_id: str | None,
        since: str | None,
        until: str | None,
        cursor: str | None,
        scan_limit: int,
    ) -> tuple[list[Any], dict[str, Any]]:
        if (
            not 1 <= scan_limit <= 200
            or not 1 <= size <= (1000 if kind == "stats" else 50 if kind == "workouts" else 100)
            or page < 1
        ):
            raise ValueError("invalid scan bounds")
        start, end = dates(since, until)
        try:
            scope = Scope(kind=kind, exercise_id=exercise_id, since=start, until=end, size=size)
        except ValidationError:
            raise ValueError("invalid scanner scope") from None
        if exercise_id is not None:
            HevyService._validate_id(exercise_id, "exercise_template_id")
        offset, total = (page - 1) * size, 0
        if cursor is not None:
            checkpoint = decode_cursor(cursor)
            if page != 1 or checkpoint.scope != scope:
                raise ValueError("cursor scope mismatch")
            offset, total = checkpoint.next_offset, checkpoint.total_seen
        encode_cursor(scope, offset, total)  # Reject oversized scope before IO.
        output: list[Any] = []
        seen: set[tuple[str, ...]] = set()
        scanned, batches, exhausted = 0, 0, False
        deadline = self.clock() + self.seconds
        reason: Literal[
            "output_limit", "source_exhausted", "scan_limit", "batch_limit", "deadline"
        ] = "output_limit"
        while len(output) < size:
            if scanned >= scan_limit:
                reason = "scan_limit"
                break
            if batches >= self.max_batches:
                reason = "batch_limit"
                break
            if self.clock() >= deadline or ((recorder := current.get()) and recorder.cancelled):
                reason = "deadline"
                break
            limit = min(5, size - len(output), scan_limit - scanned)
            raw = await self.batch(
                "history" if kind == "stats" else kind, exercise_id, offset, limit, deadline
            )
            rows = (
                HevyService._response_rows(raw, "history", "data")
                if kind in {"history", "stats"}
                else HevyService._response_rows(raw, "workouts", "data")
            )
            if len(rows) > limit:
                if kind not in {"history", "stats"}:
                    raise SchemaError("Source returned more than requested")
                # Live probe (2026-10-17):
                # the history endpoint ignores `limit` and always returns its
                # fixed 5-row batch over row-index offsets. Slice to the
                # requested rows and advance the cursor by what was consumed;
                # the remaining rows come back with the next fetch and remain
                # visible to later pages (no tail is skipped).
                rows = rows[:limit]
            if kind not in {"history", "stats"}:
                HevyService._pagination_value(raw, "page", 1)
                HevyService._pagination_value(raw, "page_count", 1)
            if kind in {"history", "stats"}:
                parsed: list[Any] = await HevyService(RowsAdapter(rows)).get_exercise_history(
                    exercise_id or ""
                )
            else:
                parsed = [
                    HevyService._parse_workout_summary(row)
                    if kind == "workouts"
                    else parse_workout(row)
                    for row in rows
                ]
            batches += 1
            for item in parsed:
                offset += 1
                total += 1
                scanned += 1
                key = (
                    (item.workout_id, item.exercise_template_id)
                    if kind in {"history", "stats"}
                    else (item.id,)
                )
                if key in seen:
                    continue
                seen.add(key)
                timestamp = item.started_at
                if (start and timestamp < normalize_timestamp(start)) or (
                    end and timestamp > normalize_timestamp(end)
                ):
                    continue
                output.append(item)
            if len(rows) < limit:
                exhausted, reason = True, "source_exhausted"
                break
        truncated = reason in {"scan_limit", "batch_limit", "deadline"}
        next_offset = None if exhausted else offset
        metadata = Checkpoint(
            next_offset=next_offset,
            next_page=offset // size + 1
            if not exhausted and offset % size == 0 and kind not in {"recent", "stats"}
            else None,
            next_cursor=None if exhausted else encode_cursor(scope, offset, total),
            has_more=not exhausted,
            total_seen=total,
            scanned_this_call=scanned,
            returned_count=len(output),
            source_exhausted=exhausted,
            scan_complete=not truncated,
            truncated=truncated,
            window_applied="client" if start or end else "none",
            since=start,
            until=end,
            stop_reason=reason,
        ).model_dump(mode="json")
        return output, metadata

    async def read(
        self,
        tool: str,
        *,
        format: Format = None,
        include_raw: bool | None = None,
        page: int = 1,
        page_size: int = 10,
        limit: int = 10,
        exercise_template_id: str | None = None,
        resource_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
        scan_limit: int = 200,
        include_meta: bool = False,
        include_stats: bool = False,
    ) -> tuple[Any, dict[str, Any] | None]:
        meta: dict[str, Any] = {}
        if tool == "get_exercise_stats":
            if exercise_template_id is None:
                raise ValueError("missing exercise_template_id")
            HevyService._validate_id(exercise_template_id, "exercise_template_id")
            source, meta = await self.scan(
                "stats",
                page=1,
                size=limit,
                exercise_id=exercise_template_id,
                since=since,
                until=until,
                cursor=cursor,
                scan_limit=scan_limit,
            )
            scope = history_scope(source, meta, origin=cursor is None)
            scope.pop("records_are_all_time")
            value = {
                "exercise_template_id": exercise_template_id,
                "stats": statistics(source).model_dump(mode="json"),
                "scope": scope,
                "records_are_all_time": False,
                **meta,
            }
            if include_raw:
                value["history"] = [item.model_dump(mode="json") for item in source]
            return value, {"hevy": meta}
        scanner = (
            any(x is not None for x in (since, until, cursor)) or include_meta or scan_limit != 200
        )
        if tool in {"get_workout", "get_routine"}:
            item = await getattr(self.service, tool)(resource_id)
            return project(item, format, include_raw, detail=True), None
        if tool == "list_routines":
            source = await self.service.list_routines()
            items = [project(item, format, include_raw) for item in source]
            if include_meta:
                return {
                    "items": items,
                    "returned_count": len(items),
                    "source_exhausted": True,
                    "window_applied": "none",
                }, None
            return items, None
        if scanner:
            kind: Literal["workouts", "recent", "history"] = (
                "workouts"
                if tool == "list_workouts"
                else "recent"
                if tool == "get_recent_workouts"
                else "history"
            )
            source, meta = await self.scan(
                kind,
                page=page,
                size=limit if kind == "recent" else page_size,
                exercise_id=exercise_template_id,
                since=since,
                until=until,
                cursor=cursor,
                scan_limit=scan_limit,
            )
            page_count = 1
        elif tool == "list_workouts":
            result = await self.service.list_workouts(page, page_size)
            source, page, page_count = result.items, result.page, result.page_count
        elif tool == "get_recent_workouts":
            source = await self.service.get_recent_workouts(limit)
        else:
            source = await self.service.get_exercise_history(exercise_template_id, page, page_size)
        items = []
        if (
            tool == "list_workouts"
            and (format == "full" or include_raw is True)
            and len(source) > page_size
        ):
            raise SchemaError("Hydration exceeds page bound")
        for item in source:
            if (recorder := current.get()) and recorder.cancelled:
                import asyncio

                raise asyncio.CancelledError
            if tool == "list_workouts" and (format == "full" or include_raw is True):
                detail = await self.service.get_workout(item.id)
                if detail.id != item.id:
                    from .errors import IdentityMismatchError

                    raise IdentityMismatchError("Hydrated workout identity mismatch")
                # Summary hydration preserves the historical summary's fields.
                if format != "full":
                    value = project(item, format, False)
                    value["raw"] = detail.raw
                else:
                    value = project(detail, format, include_raw)
            else:
                value = project(item, format, include_raw, include_stats=include_stats)
            items.append(value)
        stats: dict[str, Any] = {}
        if include_stats and tool == "get_exercise_history":
            stats = statistics(source).model_dump(mode="json")
            scope = history_scope(source, meta, origin=page == 1 and cursor is None)
            meta["stats_scope"] = scope
        if tool == "list_workouts":
            value = {"items": items, "page": page, "page_count": page_count}
            if include_meta:
                value.update(meta)
        elif include_meta:
            value = {"items": items, **{k: v for k, v in meta.items() if k != "stats_scope"}}
            if include_stats:
                value.update(stats=stats, scope=meta["stats_scope"])
        else:
            return items, {"hevy": meta} if meta else None
        return value, {"hevy": meta} if meta else None


def history_scope(
    source: list[HistoryEntry], meta: dict[str, Any], *, origin: bool
) -> dict[str, Any]:
    times: list[datetime] = [x.started_at for x in source]
    return {
        "kind": "returned_history",
        "since": meta.get("since"),
        "until": meta.get("until"),
        "entries": len(source),
        "sets": sum(len(x.sets) for x in source),
        "oldest_started_at": min(times).isoformat().replace("+00:00", "Z") if times else None,
        "newest_started_at": max(times).isoformat().replace("+00:00", "Z") if times else None,
        "started_from_origin": origin,
        "source_exhausted": meta.get("source_exhausted", False),
        "all_accessible_history_included": origin
        and not meta.get("since")
        and not meta.get("until")
        and meta.get("source_exhausted", False),
        "upstream_history_complete": None,
        "records_are_all_time": False,
    }
