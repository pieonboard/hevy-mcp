"""Typed failure documents and explicit legacy-compatible protocol emission."""

from typing import Annotated, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult as WireResult
from pydantic import BaseModel, ConfigDict, Field, RootModel

type ErrorCode = Literal[
    "HEVY_ERROR",
    "AUTHENTICATION_ERROR",
    "AUTHORIZATION_ERROR",
    "NOT_FOUND",
    "CONFLICT",
    "MUTATION_INDETERMINATE",
    "RATE_LIMITED",
    "TRANSPORT_ERROR",
    "UPSTREAM_UNAVAILABLE",
    "SCHEMA_ERROR",
    "ENDPOINT_CHANGED",
    "BACKEND_DRIFT",
    "IDENTITY_MISMATCH",
    "SNAPSHOT_ERROR",
    "CONFIGURATION_ERROR",
    "INVALID_ARGUMENT",
    "CAPABILITY_UNAVAILABLE",
    "CATALOG_UNAVAILABLE",
    "BUSY",
    "INTERNAL_ERROR",
]
# Informational permission for a later deliberate read, never a replay directive.
type RetryableHint = bool
type Phase = Literal[
    "validation", "session", "preflight", "snapshot", "mutation", "response", "verification"
]


class StrictView(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorContext(StrictView):
    tool: str | None = Field(default=None, exclude_if=lambda v: v is None)
    phase: Phase | None = Field(default=None, exclude_if=lambda v: v is None)
    correlation_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    resource_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    field_names: list[str] | None = Field(default=None, exclude_if=lambda v: v is None)
    cause_code: ErrorCode | None = Field(default=None, exclude_if=lambda v: v is None)


class ErrorView(StrictView):
    code: ErrorCode
    message: str
    hint: str
    retryable: RetryableHint
    retry_after_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    context: ErrorContext | None = Field(default=None, exclude_if=lambda v: v is None)


# Design terminology and caller-facing spelling describe the same wire object.
ErrorDetail = ErrorView


class MutationOutcome(StrictView):
    operation: Literal["create", "update", "delete"]
    resource_type: Literal["routine", "workout", "routine_folder"]
    resource_id: str | None
    phase: Phase
    request_state: Literal["not_attempted", "may_have_been_sent", "acknowledged"]
    effect: Literal["not_applied", "applied", "unknown", "still_present"]
    verification: Literal["not_run", "verified", "absent", "present", "mismatch", "inconclusive"]
    snapshot_path: str | None
    backend: str | None
    mutation_attempts: Annotated[int, Field(ge=0, le=1)] | None = None
    automatic_replay: Literal[False] = False
    cause_code: ErrorCode | None = None


class Failure(StrictView):
    error: ErrorView


class MutationErrorView(ErrorView):
    retryable: Literal[False]


class PartialMutationFailure(Failure):
    error: MutationErrorView
    outcome: MutationOutcome


class ToolResult[T: BaseModel](RootModel[T | Failure | PartialMutationFailure]):
    """Object-root schema union; this is separate from FastMCP's transport result."""

    model_config = ConfigDict(json_schema_extra={"type": "object"})


class HevyToolError(ToolError):
    """Preserve typed safe state when FastMCP wraps ordinary domain exceptions."""

    def __init__(self, failure: Failure | PartialMutationFailure) -> None:
        self.failure = failure
        super().__init__("Hevy operation failed")


def emit(
    value: object, *, meta: dict[str, object] | None = None, is_error: bool = False
) -> "WireResult":
    """Preserve SDK per-item text blocks and the structured array wrapper."""
    import json

    from fastmcp.tools import ToolResult as WireResult
    from mcp.types import TextContent

    blocks = value if isinstance(value, list) else [value]
    return WireResult(
        content=[TextContent(type="text", text=json.dumps(x, ensure_ascii=False)) for x in blocks],
        structured_content={"result": value} if isinstance(value, list) else value,
        meta=meta,
        is_error=is_error,
    )
