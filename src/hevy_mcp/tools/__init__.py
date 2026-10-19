"""Ten explicit public tools and shared registration metadata."""

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from ..boundary import classify, failure
from ..catalog import Catalog
from ..models import RoutineInput
from ..reads import Reads
from ..results import HevyToolError, emit
from ..runtime import Runtime
from ..schemas import output_schema, validate_output

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

TOOL_DESCRIPTIONS = {
    "search_exercise_templates": "Search the validated local exercise catalog before choosing exercise_template_id. Offline, no credentials or remote catalog scrape; tokens match titles, exact muscle/equipment filters. Inspect provenance/stale; include_custom=true is capability-unavailable until its parsing is evidenced.",
    "get_exercise_stats": "Compute scoped estimated e1RM, heaviest/max-reps source sets, volume and rep zones from bounded history using the same local algorithm as include_stats. Default 100, max 1000 observations; at most 200 source rows/40 batches/120 seconds per call. Continue next_cursor with the same size/window; records apply only to this batch, not all-time. include_raw=true retains history for audit/merging by IDs.",
    "list_workouts": "List compact workout summaries and IDs, page_size up to 50. Use get_workout for complete sets. Use include_meta=true for client-side window scans and reuse next_cursor with the same window/size. Full mode or include_raw=true hydrates at most 50 singular details; has_more is conservative.",
    "get_workout": "Retrieve one complete workout with exercises, sets, notes, canonical ID/title/start time and follow-up IDs. Use those exact values before delete_workout. include_raw=false saves tokens; summary mode is explicit.",
    "get_recent_workouts": "Retrieve recent complete workouts for training context, repeated exercises and load selection: default 10, maximum 100. Explicit summary mode saves tokens; date windows filter client-side within bounded scans. This is not all history.",
    "get_exercise_history": "Retrieve normalized historical working sets for progression and previous loads/reps. include_stats=true adds local estimated metrics without extra reads; records describe returned/scanned history, not necessarily all-time. Use include_meta=true and next_cursor for bounded date windows.",
    "list_routines": "List normalized Hevy routines and resolve IDs; summary mode saves tokens. Always get_routine before full replacement; project response extras into RoutineInput-supported fields.",
    "get_routine": "Read a complete routine; always call before update because update is a full replacement. Project output extras into the strict input schema and preserve supported fields that should remain.",
    "create_routine": "Create a new routine from structured exercises and sets: a real write, one attempt followed by singular read-back. Accepted versus transmitted field limitations are documented in docs/DESIGN.md §4.3; inspect unknown outcomes before another action.",
    "update_routine": "Perform a full replacement of a routine including exercises and sets, with one write attempt and read-back. Call get_routine first, project into RoutineInput and preserve every supported field that should remain. Accepted versus transmitted field limitations: docs/DESIGN.md §4.3.",
    "delete_routine": "Permanently delete a routine only after an explicit user request, then verify absence. There is no workout-style snapshot guarantee; inspect unknown outcomes before another action.",
    "delete_workout": "Permanently delete one specific Hevy workout after verifying its ID, exact title and start timestamp. Only call when the user explicitly requested deletion of that identified workout. Never infer deletion because a workout looks duplicated, incorrect, empty or unusual. Requires a configured backend, persists a private snapshot before one DELETE, then verifies absence; no fallback or replay.",
}


class Facade:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.reads = Reads(runtime)
        self.catalog = Catalog()

    def search(self, **options: Any) -> ToolResult:
        def action() -> ToolResult:
            value = self.catalog.search(**options)
            validate_output("search_exercise_templates", value)
            return emit(value)

        return self.local("search_exercise_templates", action)

    @staticmethod
    def local[T](tool: str, action: Callable[[], T]) -> T:
        """Same typed bridge as account reads, with no session/lane admission."""
        try:
            return action()
        except HevyToolError:
            raise
        except Exception as exc:
            code = (
                "SCHEMA_ERROR"
                if isinstance(exc, ValidationError)
                else "INVALID_ARGUMENT"
                if isinstance(exc, ValueError)
                else classify(exc)
            )
            raise HevyToolError(failure(code, tool=tool)) from None

    async def read(self, name: str, **options: Any) -> ToolResult:
        async def action() -> ToolResult:
            value, meta = await self.reads.read(name, **options)
            validate_output(name, value)
            return emit(value, meta=meta)

        return await self.runtime.invoke(name, action, resource_id=options.get("resource_id"))

    async def write(self, name: str, *args: str | RoutineInput) -> ToolResult:
        async def action() -> ToolResult:
            result = await getattr(self.runtime.service, name)(*args)
            value = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            validate_output(name, value)
            return emit(value)

        return await self.runtime.invoke(
            name, action, resource_id=args[0] if args and isinstance(args[0], str) else None
        )


def register(server: FastMCP, fn: Any, *, annotation: ToolAnnotations) -> None:
    server.add_tool(
        Tool.from_function(
            fn,
            description=TOOL_DESCRIPTIONS[fn.__name__],
            annotations=annotation,
            output_schema=output_schema(fn.__name__),
        )
    )
