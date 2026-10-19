from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, ClassVar, Protocol, cast
from uuid import uuid4

from pydantic import ValidationError

from .auth import Session, SessionManager
from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    EndpointChangedError,
    HevyError,
    IdentityMismatchError,
    MutationIndeterminateError,
    NotFoundError,
    RateLimitError,
    RetryableUpstreamError,
    SchemaError,
    TransportError,
)
from .models import (
    DeletionResult,
    HistoryEntry,
    Routine,
    RoutineInput,
    Workout,
    WorkoutPage,
    WorkoutSummary,
    normalize_timestamp,
    parse_routine,
    parse_workout,
)


class Adapter(Protocol):
    async def call(self, name: str, *args: Any, **kwargs: Any) -> Any: ...


def _private_routine_payload(routine: RoutineInput, *, index: int) -> dict[str, Any]:
    """Convert common public routine fields to Hevy's private app DTO."""
    exercises: list[dict[str, Any]] = []
    for exercise in routine.exercises:
        sets: list[dict[str, Any]] = []
        for set_index, routine_set in enumerate(exercise.sets):
            private_set: dict[str, Any] = {
                "index": set_index,
                "indicator": routine_set.type,
            }
            for key in ("weight_kg", "reps", "distance_meters", "duration_seconds"):
                value = getattr(routine_set, key)
                if value is not None:
                    private_set[key] = value
            sets.append(private_set)

        private_exercise: dict[str, Any] = {
            "exercise_template_id": exercise.exercise_template_id,
            "notes": exercise.notes or "",
            "rest_seconds": exercise.rest_seconds or 0,
            "sets": sets,
        }
        if exercise.superset_id is not None:
            private_exercise["superset_id"] = exercise.superset_id
        exercises.append(private_exercise)

    return {
        "title": routine.title,
        "folder_id": routine.folder_id,
        "index": index,
        "notes": routine.notes,
        "program_id": routine.program_id,
        "exercises": exercises,
    }


def private_create_routine_payload(routine: RoutineInput) -> dict[str, Any]:
    """Convert the public MCP model to Hevy's private app create DTO."""
    body = _private_routine_payload(routine, index=-1)
    client_id = str(uuid4())
    body["_unsyncedObjectId"] = client_id
    body["clientId"] = client_id
    return body


def private_update_routine_payload(routine: RoutineInput) -> dict[str, Any]:
    """Convert the public MCP model to Hevy's private app update DTO."""
    return _private_routine_payload(routine, index=routine.index)


def map_upstream_error(exc: Exception) -> HevyError:
    """Translate hevy-unofficial/httpx failures into stable service errors."""
    try:
        from hevy_unofficial.exceptions import HevyAPIError, HevyRateLimitError

        if isinstance(exc, HevyRateLimitError):
            return RateLimitError("Hevy rate limit exceeded")
        if isinstance(exc, HevyAPIError):
            mapped = {
                401: AuthenticationError("Hevy session rejected"),
                403: AuthorizationError("Hevy request forbidden"),
                404: NotFoundError("Hevy resource not found"),
                409: ConflictError("Hevy conflict"),
                429: RateLimitError("Hevy rate limit exceeded"),
            }.get(exc.status_code)
            if mapped is not None:
                return mapped
            if 500 <= exc.status_code <= 599:
                return RetryableUpstreamError(f"Hevy transient server failure ({exc.status_code})")
            return EndpointChangedError(f"Hevy endpoint failed ({exc.status_code})")
    except ImportError:
        pass
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
            return TransportError("Hevy request timed out or failed")
    except ImportError:
        pass
    return EndpointChangedError(f"Unexpected Hevy client failure: {type(exc).__name__}")


class UnofficialAdapter:
    def __init__(self, client: Any):
        self.client = client

    async def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        def invoke() -> Any:
            match name:
                case "list_workouts":
                    # Live-verified 2026-09-08: Hevy rejects limit > 5 on
                    # /user_workouts_paged with 400 and ignores since/until, so
                    # page in fixed batches of five and never send date filters.
                    username = kwargs.pop("username", None) or self._authenticated_username()
                    page = int(kwargs.pop("page", 1))
                    limit = int(kwargs.pop("limit", 10))
                    rows: list[Any] = []
                    offset = (page - 1) * limit
                    batch_size = min(limit, 5)
                    while len(rows) < limit:
                        batch = self.client.workouts.list_paged(
                            username=username, limit=batch_size, offset=offset + len(rows)
                        )
                        inner = (
                            batch.get("workouts", batch.get("data"))
                            if isinstance(batch, dict)
                            else batch
                        )
                        if not isinstance(inner, list):
                            return batch
                        rows.extend(inner)
                        if len(inner) < batch_size:
                            break
                    return {"workouts": rows[:limit]}
                case "get_workout":
                    return self.client.workouts.get(args[0])
                case "exercise_history":
                    page = int(kwargs.pop("page", 1))
                    limit = int(kwargs.pop("limit", 20))
                    start = (page - 1) * limit
                    rows = []
                    while len(rows) < limit:
                        params = {
                            "exerciseTemplateId": args[0],
                            "limit": limit,
                            "offset": start + len(rows),
                            **{key: value for key, value in kwargs.items() if value is not None},
                        }
                        batch = self.client.exercises.history_paged(params)
                        if not isinstance(batch, list):
                            return batch
                        rows.extend(batch)
                        if len(batch) < 5:
                            break
                    return rows[:limit]
                case "list_routines":
                    return self.client.routines.list()
                case "get_routine":
                    return self.client.routines.get(args[0])
                case "create_routine":
                    return self.client.routines.create(args[0])
                case "update_routine":
                    return self.client.routines.update(args[0], args[1])
                case "delete_routine":
                    return self.client.routines.delete(args[0])
                case _:
                    raise ValueError(name)

        try:
            return await asyncio.to_thread(invoke)
        except Exception as exc:
            mapped = map_upstream_error(exc)
            if name in {"create_routine", "update_routine", "delete_routine"} and isinstance(
                mapped,
                AuthenticationError | RateLimitError | RetryableUpstreamError | TransportError,
            ):
                reason = (
                    "auth"
                    if isinstance(mapped, AuthenticationError)
                    else "rate_limit"
                    if isinstance(mapped, RateLimitError)
                    else "transient_server"
                    if isinstance(mapped, RetryableUpstreamError)
                    else "transport"
                )
                raise MutationIndeterminateError(
                    "Routine mutation failed ambiguously "
                    f"(reason={reason}); request was not replayed and outcome is indeterminate"
                ) from exc
            raise mapped from exc

    def _authenticated_username(self) -> str:
        account = self.client.users.get_account()
        if not isinstance(account, dict):
            raise TypeError("account response is not an object")
        candidates = [account]
        candidates.extend(
            value
            for key in ("user", "account", "profile")
            if isinstance((value := account.get(key)), dict)
        )
        for candidate in candidates:
            for key in ("username", "userName"):
                username = candidate.get(key)
                if username and str(username).strip():
                    return str(username)
        raise ValueError("authenticated account username is missing")


class PersistentUnofficialAdapter:
    """Create short-lived no-auto-refresh clients around the single local session store."""

    READS: ClassVar[set[str]] = {
        "list_workouts",
        "get_workout",
        "exercise_history",
        "list_routines",
        "get_routine",
    }

    def __init__(
        self,
        sessions: SessionManager,
        client_factory: Callable[[Session], Any] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sessions = sessions
        self.client_factory = client_factory or self._client
        self.sleeper = sleeper

    @staticmethod
    def _client(session: Session) -> Any:
        from hevy_unofficial import HevyClient
        from hevy_unofficial.config import HevyConfig

        return HevyClient(
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            user_id=session.user_id,
            config=HevyConfig(auto_refresh=False),
        )

    async def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        session = await self.sessions.valid_session()
        for attempt in range(3):
            client = self.client_factory(session)
            try:
                return await UnofficialAdapter(client).call(name, *args, **kwargs)
            except AuthenticationError:
                if name not in self.READS or attempt:
                    raise
                session = await self.sessions.refresh(expected_access_token=session.access_token)
            except (RateLimitError, RetryableUpstreamError, TransportError):
                if name not in self.READS or attempt == 2:
                    raise
                await self.sleeper(0.1 * (2**attempt))
            finally:
                close = getattr(client, "close", None)
                if close:
                    await asyncio.to_thread(close)
        raise AssertionError("unreachable")


class HevyService:
    def __init__(self, adapter: Adapter, deletion: Any | None = None):
        self.adapter = adapter
        self.deletion = deletion

    def validate_recent_limit(self, limit: int) -> int:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return limit

    @staticmethod
    def _validate_id(value: str, name: str) -> str:
        if not value or not value.strip():
            raise ValueError(f"{name} must not be empty")
        return value

    @staticmethod
    def _validate_dates(since: str | None, until: str | None) -> None:
        try:
            start = normalize_timestamp(since) if since else None
            end = normalize_timestamp(until) if until else None
        except (TypeError, ValueError) as exc:
            raise ValueError("date filters must be ISO-8601 timestamps") from exc
        if start and end and start > end:
            raise ValueError("since must not be after until")

    @staticmethod
    def _response_rows(raw: Any, *keys: str) -> list[Any]:
        if isinstance(raw, list):
            return raw
        if not isinstance(raw, dict):
            raise SchemaError("Hevy list response schema changed: expected an object or list")
        for key in keys:
            if key in raw:
                rows = raw[key]
                if not isinstance(rows, list):
                    raise SchemaError(f"Hevy list response schema changed: {key} is not a list")
                return rows
        raise SchemaError("Hevy list response schema changed: expected list container")

    @staticmethod
    def _pagination_value(raw: Any, key: str, default: int) -> int:
        if not isinstance(raw, dict) or key not in raw:
            return default
        value = raw[key]
        if isinstance(value, bool):
            raise SchemaError(f"Workout pagination schema changed: invalid {key}")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"Workout pagination schema changed: invalid {key}") from exc
        if parsed < 1:
            raise SchemaError(f"Workout pagination schema changed: invalid {key}")
        return parsed

    async def list_workouts(
        self, page: int = 1, page_size: int = 10, since: str | None = None, until: str | None = None
    ) -> WorkoutPage:
        if page < 1 or not 1 <= page_size <= 50:
            raise ValueError("invalid pagination")
        self._validate_dates(since, until)
        raw = await self.adapter.call(
            "list_workouts", page=page, limit=page_size, since=since, until=until
        )
        rows = self._response_rows(raw, "workouts", "data")
        workouts = [self._parse_workout_summary(x) for x in rows]
        return WorkoutPage(
            items=workouts,
            page=self._pagination_value(raw, "page", page),
            page_count=self._pagination_value(raw, "page_count", 1),
        )

    @staticmethod
    def _parse_workout_summary(raw: Any) -> WorkoutSummary:
        try:
            if not isinstance(raw, dict):
                raise TypeError("summary is not an object")
            raw_id = raw.get("id", raw.get("workout_id"))
            title = raw.get("title", raw.get("name"))
            started = raw.get("start_time", raw.get("started_at"))
            if raw_id is None or not str(raw_id).strip():
                raise ValueError("workout ID missing")
            if title is None or not str(title).strip():
                raise ValueError("workout title missing")
            if started is None:
                raise ValueError("workout start timestamp missing")
            ended_raw = raw.get("end_time", raw.get("ended_at"))
            started_at = normalize_timestamp(started)
            ended_at = normalize_timestamp(ended_raw) if ended_raw else None
            exercises = raw.get("exercises")
            count = raw.get("exercise_count", len(exercises) if isinstance(exercises, list) else 0)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise TypeError("exercise_count is invalid")
            return WorkoutSummary(
                id=str(raw_id),
                title=str(title),
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=int((ended_at - started_at).total_seconds()) if ended_at else None,
                exercise_count=count,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise SchemaError(f"Workout summary schema changed: {exc}") from exc

    async def get_workout(self, workout_id: str) -> Workout:
        self._validate_id(workout_id, "workout_id")
        return parse_workout(await self.adapter.call("get_workout", workout_id))

    async def get_recent_workouts(self, limit: int = 10) -> list[Workout]:
        self.validate_recent_limit(limit)
        raw = await self.adapter.call("list_workouts", page=1, limit=limit)
        return [parse_workout(x) for x in self._response_rows(raw, "workouts", "data")][:limit]

    async def get_exercise_history(
        self,
        exercise_template_id: str,
        page: int = 1,
        page_size: int = 20,
        since: str | None = None,
        until: str | None = None,
    ) -> list[HistoryEntry]:
        self._validate_id(exercise_template_id, "exercise_template_id")
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("invalid pagination")
        self._validate_dates(since, until)
        raw = await self.adapter.call(
            "exercise_history",
            exercise_template_id,
            page=page,
            limit=page_size,
            since=since,
            until=until,
        )
        rows = self._response_rows(raw, "history", "data")
        try:
            entries = []
            for row in rows:
                if not isinstance(row, dict):
                    raise TypeError("history row is not an object")
                workout_id = row.get("workout_id", row.get("id"))
                template_id = row.get("exercise_template_id", exercise_template_id)
                if workout_id is None or not str(workout_id).strip():
                    raise ValueError("history workout ID missing")
                if template_id is None or not str(template_id).strip():
                    raise ValueError("history exercise template ID missing")
                started_at = row.get("started_at", row.get("start_time"))
                if not isinstance(started_at, str | int | float | datetime) or isinstance(
                    started_at, bool
                ):
                    raise TypeError("history start timestamp missing or invalid")
                sets = row.get("sets")
                if sets is None:
                    exercises = row.get("exercises")
                    if (
                        not isinstance(exercises, list)
                        or not exercises
                        or not isinstance(exercises[0], dict)
                        or not isinstance(exercises[0].get("sets"), list)
                    ):
                        raise TypeError("history exercise sets missing or invalid")
                    sets = exercises[0]["sets"]
                if not isinstance(sets, list):
                    raise TypeError("history sets are not a list")
                entries.append(
                    HistoryEntry(
                        workout_id=str(workout_id),
                        exercise_template_id=str(template_id),
                        started_at=normalize_timestamp(started_at),
                        sets=sets,
                        raw=row,
                    )
                )
            return entries
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise SchemaError("Exercise history schema changed") from exc

    async def list_routines(self) -> list[Routine]:
        raw = await self.adapter.call("list_routines")
        required = {"updated", "deleted", "isMore"}
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise SchemaError(
                "Routine sync schema changed: expected updated/deleted/isMore envelope"
            )
        if (
            not isinstance(raw["updated"], list)
            or not isinstance(raw["deleted"], list)
            or not isinstance(raw["isMore"], bool)
        ):
            raise SchemaError("Routine sync schema changed: invalid updated/deleted/isMore fields")
        if raw["isMore"]:
            raise SchemaError("Routine sync response has isMore=true; refusing incomplete results")
        rows = raw["updated"]
        return [parse_routine(x) for x in rows]

    async def get_routine(self, routine_id: str) -> Routine:
        self._validate_id(routine_id, "routine_id")
        return parse_routine(await self.adapter.call("get_routine", routine_id))

    async def create_routine(self, routine: RoutineInput | dict[str, Any]) -> Routine:
        validated = RoutineInput.model_validate(routine)
        response = await self.adapter.call(
            "create_routine", private_create_routine_payload(validated)
        )
        if isinstance(response, dict) and "routineId" in response:
            value = response["routineId"]
            if not isinstance(value, str) or not value.strip():
                raise SchemaError("Hevy create response routineId missing or invalid")
            routine_id = value
        else:
            routine_id = parse_routine(response).id
        readback = await self.get_routine(routine_id)
        if readback.id != routine_id:
            raise IdentityMismatchError("Created routine read-back returned a different ID")
        return readback

    async def update_routine(
        self, routine_id: str, routine: RoutineInput | dict[str, Any]
    ) -> Routine:
        self._validate_id(routine_id, "routine_id")
        validated = RoutineInput.model_validate(routine)
        body = private_update_routine_payload(validated)
        await self.adapter.call("update_routine", routine_id, body)
        readback = await self.get_routine(routine_id)
        if readback.id != routine_id:
            raise IdentityMismatchError("Updated routine read-back returned a different ID")
        return readback

    async def delete_routine(self, routine_id: str) -> dict[str, Any]:
        self._validate_id(routine_id, "routine_id")
        await self.adapter.call("delete_routine", routine_id)
        try:
            await self.get_routine(routine_id)
        except NotFoundError:
            return {"deleted": True, "routine_id": routine_id}
        raise EndpointChangedError("Routine DELETE returned but read-back still finds the routine")

    async def delete_workout(
        self, workout_id: str, expected_title: str, expected_started_at: str
    ) -> DeletionResult:
        if not self.deletion:
            raise RuntimeError("workout delete backend is not configured")
        return cast(
            DeletionResult,
            await self.deletion.delete(workout_id, expected_title, expected_started_at),
        )
