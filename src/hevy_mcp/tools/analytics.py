"""B5: scoped records from the same finite history scanner and A3 reducer."""

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from . import READ_ONLY, Facade, register


def register_tools(server: FastMCP, facade: Facade) -> None:
    async def get_exercise_stats(
        exercise_template_id: str,
        since: str | None = None,
        until: str | None = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
        cursor: str | None = None,
        scan_limit: Annotated[int, Field(ge=1, le=200)] = 200,
        include_raw: bool = False,
    ) -> ToolResult:
        return await facade.read(
            "get_exercise_stats",
            exercise_template_id=exercise_template_id,
            since=since,
            until=until,
            limit=limit,
            cursor=cursor,
            scan_limit=scan_limit,
            include_raw=include_raw,
        )

    register(server, get_exercise_stats, annotation=READ_ONLY)
