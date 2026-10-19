"""Safe error vocabulary and the two protocol boundaries."""

from typing import Any

from fastmcp.exceptions import ValidationError as ArgumentValidationError
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import CallToolRequestParams, ReadResourceRequestParams

from . import errors
from .results import ErrorCode, ErrorContext, ErrorView, Failure, HevyToolError, emit

# Order matters: BackendDriftError is an EndpointChangedError.
ERRORS: list[tuple[type[Exception], ErrorCode, bool, str]] = [
    (
        errors.AuthenticationError,
        "AUTHENTICATION_ERROR",
        False,
        "Run hevy-mcp auth status; if needed run auth login separately, then inspect the target before repeating a mutation.",
    ),
    (
        errors.AuthorizationError,
        "AUTHORIZATION_ERROR",
        False,
        "Check account permission and target ownership; login may not fix 403.",
    ),
    (
        errors.NotFoundError,
        "NOT_FOUND",
        False,
        "Verify the exact ID or whether the object was removed.",
    ),
    (
        errors.ConflictError,
        "CONFLICT",
        False,
        "Read the current target and reconcile intent before submitting again.",
    ),
    (
        errors.MutationIndeterminateError,
        "MUTATION_INDETERMINATE",
        False,
        "Inspect the known target and snapshot; the request was not replayed.",
    ),
    (errors.RateLimitError, "RATE_LIMITED", True, "Wait, then retry the read deliberately."),
    (
        errors.TransportError,
        "TRANSPORT_ERROR",
        True,
        "Check connectivity; retry the read later and inspect any possibly delivered write.",
    ),
    (
        errors.RetryableUpstreamError,
        "UPSTREAM_UNAVAILABLE",
        True,
        "Retry the read later after transient upstream failure.",
    ),
    (
        errors.SchemaError,
        "SCHEMA_ERROR",
        False,
        "Preserve sanitized shape evidence and update the adapter; malformed data is not empty.",
    ),
    (
        errors.BackendDriftError,
        "BACKEND_DRIFT",
        False,
        "Inspect snapshot and target; verify the configured backend manually, without fallback.",
    ),
    (
        errors.EndpointChangedError,
        "ENDPOINT_CHANGED",
        False,
        "Review pinned endpoint behavior; do not switch write endpoints automatically.",
    ),
    (
        errors.IdentityMismatchError,
        "IDENTITY_MISMATCH",
        False,
        "Re-read canonical ID, title and start time; never change identity just to force deletion.",
    ),
    (
        errors.SnapshotError,
        "SNAPSHOT_ERROR",
        False,
        "Fix private archive permissions, storage and fsync; no DELETE was sent if snapshot persistence failed.",
    ),
    (
        errors.ConfigurationError,
        "CONFIGURATION_ERROR",
        False,
        "Repair config; configure workout deletion with the guarded tester on a disposable session.",
    ),
    (
        errors.HevyError,
        "HEVY_ERROR",
        False,
        "Inspect operation and configuration; do not repeat a write blindly.",
    ),
]
EXTRA: dict[ErrorCode, tuple[bool, str]] = {
    "INVALID_ARGUMENT": (
        False,
        "Correct named fields and ranges; omit rejected raw input from diagnostics.",
    ),
    "INTERNAL_ERROR": (False, "Inspect sanitized logs by correlation ID."),
    "BUSY": (True, "Wait for the active operation to finish, then retry the read deliberately."),
    "CATALOG_UNAVAILABLE": (
        False,
        "Install the documented local catalog snapshot; do not guess a remote search.",
    ),
    "CAPABILITY_UNAVAILABLE": (
        False,
        "Use the supported surface or complete its documented evidence gate.",
    ),
}


def failure(
    code: ErrorCode,
    *,
    tool: str | None = None,
    mutation: bool = False,
    context: ErrorContext | None = None,
) -> Failure:
    retryable, hint = EXTRA.get(
        code,
        next(((r, h) for _, c, r, h in ERRORS if c == code), (False, "Inspect sanitized logs.")),
    )
    return Failure(
        error=ErrorView(
            code=code,
            message=code.replace("_", " ").capitalize(),
            hint=hint,
            retryable=retryable and not mutation,
            context=context or ErrorContext(tool=tool),
        )
    )


def classify(exc: Exception) -> ErrorCode:
    for cls, code, _, _ in ERRORS:
        if isinstance(exc, cls):
            return code
    return "INTERNAL_ERROR"


def resource_result(value: dict[str, Any], meta: dict[str, Any] | None = None) -> ResourceResult:
    import json

    return ResourceResult(
        [ResourceContent(json.dumps(value, ensure_ascii=False), mime_type="application/json")],
        meta=meta,
    )


class ErrorBoundary(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except HevyToolError as exc:
            return emit(exc.failure.model_dump(mode="json"), is_error=True)
        except ArgumentValidationError:
            return emit(
                failure("INVALID_ARGUMENT", tool=context.message.name).model_dump(mode="json"),
                is_error=True,
            )

    async def on_read_resource(
        self,
        context: MiddlewareContext[ReadResourceRequestParams],
        call_next: CallNext[ReadResourceRequestParams, ResourceResult],
    ) -> ResourceResult:
        try:
            from .resources import validate_uri

            try:
                validate_uri(str(context.message.uri))
            except (ValueError, UnicodeError):
                raise HevyToolError(failure("INVALID_ARGUMENT")) from None
            return await call_next(context)
        except HevyToolError as exc:
            return resource_result(
                exc.failure.model_dump(mode="json"), {"hevy": {"status": "error"}}
            )
