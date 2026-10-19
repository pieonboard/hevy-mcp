"""Historical workout signatures followed by additive A options."""

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from ..reads import Format
from . import DESTRUCTIVE, READ_ONLY, Facade, register


def register_tools(server: FastMCP, facade: Facade) -> None:
    async def list_workouts(
        page: Annotated[int, Field(ge=1)] = 1,
        page_size: Annotated[int, Field(ge=1, le=50)] = 10,
        *,
        format: Format = None,
        include_raw: bool | None = None,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
        scan_limit: Annotated[int, Field(ge=1, le=200)] = 200,
        include_meta: bool = False,
    ) -> ToolResult:
        return await facade.read(
            "list_workouts",
            page=page,
            page_size=page_size,
            format=format,
            include_raw=include_raw,
            since=since,
            until=until,
            cursor=cursor,
            scan_limit=scan_limit,
            include_meta=include_meta,
        )

    async def get_workout(
        workout_id: str, *, format: Format = None, include_raw: bool | None = None
    ) -> ToolResult:
        return await facade.read(
            "get_workout", resource_id=workout_id, format=format, include_raw=include_raw
        )

    async def get_recent_workouts(
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        *,
        format: Format = None,
        include_raw: bool | None = None,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
        scan_limit: Annotated[int, Field(ge=1, le=200)] = 200,
        include_meta: bool = False,
    ) -> ToolResult:
        return await facade.read(
            "get_recent_workouts",
            limit=limit,
            format=format,
            include_raw=include_raw,
            since=since,
            until=until,
            cursor=cursor,
            scan_limit=scan_limit,
            include_meta=include_meta,
        )

    async def get_exercise_history(
        exercise_template_id: str,
        page: Annotated[int, Field(ge=1)] = 1,
        page_size: Annotated[int, Field(ge=1, le=100)] = 20,
        *,
        format: Format = None,
        include_raw: bool | None = None,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
        scan_limit: Annotated[int, Field(ge=1, le=200)] = 200,
        include_meta: bool = False,
        include_stats: bool = False,
    ) -> ToolResult:
        return await facade.read(
            "get_exercise_history",
            exercise_template_id=exercise_template_id,
            page=page,
            page_size=page_size,
            format=format,
            include_raw=include_raw,
            since=since,
            until=until,
            cursor=cursor,
            scan_limit=scan_limit,
            include_meta=include_meta,
            include_stats=include_stats,
        )

    async def delete_workout(
        workout_id: str, expected_title: str, expected_started_at: str
    ) -> ToolResult:
        return await facade.write("delete_workout", workout_id, expected_title, expected_started_at)

    for fn in (list_workouts, get_workout, get_recent_workouts, get_exercise_history):
        register(server, fn, annotation=READ_ONLY)
    register(server, delete_workout, annotation=DESTRUCTIVE)
