"""Account-independent B4; explicitly unsupported custom-template parsing."""

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from . import READ_ONLY, Facade, register


def register_tools(server: FastMCP, facade: Facade) -> None:
    async def search_exercise_templates(
        query: Annotated[str, Field(max_length=200)] = "",
        muscle_group: str | None = None,
        equipment_category: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
        include_custom: bool = False,
        include_raw: bool = False,
    ) -> ToolResult:
        return facade.search(
            query=query,
            muscle_group=muscle_group,
            equipment_category=equipment_category,
            limit=limit,
            offset=offset,
            include_custom=include_custom,
            include_raw=include_raw,
        )

    register(server, search_exercise_templates, annotation=READ_ONLY)
