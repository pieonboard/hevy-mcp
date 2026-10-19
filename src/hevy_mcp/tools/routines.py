"""Historical routine signatures, preserving full replacement semantics."""

from fastmcp import FastMCP
from fastmcp.tools import ToolResult

from ..models import RoutineInput
from ..reads import Format
from . import DESTRUCTIVE, READ_ONLY, WRITE, Facade, register


def register_tools(server: FastMCP, facade: Facade) -> None:
    async def list_routines(
        *, format: Format = None, include_raw: bool | None = None, include_meta: bool = False
    ) -> ToolResult:
        return await facade.read(
            "list_routines", format=format, include_raw=include_raw, include_meta=include_meta
        )

    async def get_routine(
        routine_id: str, *, format: Format = None, include_raw: bool | None = None
    ) -> ToolResult:
        return await facade.read(
            "get_routine", resource_id=routine_id, format=format, include_raw=include_raw
        )

    async def create_routine(routine: RoutineInput) -> ToolResult:
        return await facade.write("create_routine", routine)

    async def update_routine(routine_id: str, routine: RoutineInput) -> ToolResult:
        return await facade.write("update_routine", routine_id, routine)

    async def delete_routine(routine_id: str) -> ToolResult:
        return await facade.write("delete_routine", routine_id)

    for fn in (list_routines, get_routine):
        register(server, fn, annotation=READ_ONLY)
    for write_fn in (create_routine, update_routine):
        register(server, write_fn, annotation=WRITE)
    register(server, delete_routine, annotation=DESTRUCTIVE)
