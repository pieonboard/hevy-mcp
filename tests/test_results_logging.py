import io
import json
import logging

import jsonschema
import pytest
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult as FastMCPResult
from mcp.types import TextContent
from pydantic import BaseModel, ValidationError

from hevy_mcp.logging import configure_logging


def error_data():
    return {
        "code": "AUTHENTICATION_ERROR",
        "message": "Authentication required",
        "hint": "Run hevy-mcp auth status",
        "retryable": False,
    }


def outcome_data():
    return {
        "operation": "delete",
        "resource_type": "workout",
        "resource_id": "w1",
        "phase": "verification",
        "request_state": "may_have_been_sent",
        "effect": "unknown",
        "verification": "inconclusive",
        "snapshot_path": "/private/snapshot.json",
        "backend": "v2",
        "mutation_attempts": 1,
        "automatic_replay": False,
        "cause_code": "TRANSPORT_ERROR",
    }


def test_error_and_partial_schemas_validate_real_payloads():
    from hevy_mcp.results import Failure, PartialMutationFailure, ToolResult

    class Success(BaseModel):
        items: list[str]

    schema = ToolResult[Success].model_json_schema()
    assert schema["type"] == "object"
    jsonschema.Draft202012Validator.check_schema(schema)
    for payload in [
        {"items": []},
        {"items": ["w1"]},
        {"error": error_data()},
        {"error": error_data(), "outcome": outcome_data()},
    ]:
        jsonschema.validate(payload, schema)
        assert ToolResult[Success].model_validate(payload).model_dump(mode="json") == payload
    assert Failure.model_validate({"error": error_data()}).error.retryable is False
    assert (
        PartialMutationFailure.model_validate(
            {"error": error_data(), "outcome": outcome_data()}
        ).outcome.snapshot_path
        == "/private/snapshot.json"
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"code": "UNKNOWN"},
        {"retry_after_seconds": -1},
        {"retry_after_seconds": float("inf")},
        {"retry_after_seconds": float("nan")},
        {"context": {"tokens": "secret"}},
        {"context": {"cause_code": "UNKNOWN"}},
        {"headers": {"cookie": "secret"}},
    ],
)
def test_error_schema_rejects_unsafe_or_invalid_fields(patch):
    from hevy_mcp.results import ErrorView

    with pytest.raises(ValidationError):
        ErrorView.model_validate({**error_data(), **patch})


@pytest.mark.parametrize(
    "patch",
    [
        {"automatic_replay": True},
        {"mutation_attempts": 2},
        {"mutation_attempts": -1},
        {"phase": "guess"},
        {"effect": "probably"},
        {"headers": {"cookie": "secret"}},
    ],
)
def test_partial_schema_rejects_guesses_and_replay(patch):
    from hevy_mcp.results import MutationOutcome

    with pytest.raises(ValidationError):
        MutationOutcome.model_validate({**outcome_data(), **patch})


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_safe_bridge_survives_fake_fastmcp_call(partial):
    from hevy_mcp.results import Failure, HevyToolError, PartialMutationFailure

    payload = (
        PartialMutationFailure(error=error_data(), outcome=outcome_data())
        if partial
        else Failure(error=error_data())
    )

    class FakeBoundary(Middleware):
        # Characterization only. Production middleware is the boundary.
        async def on_call_tool(self, context, call_next):
            try:
                return await call_next(context)
            except HevyToolError as exc:
                data = exc.failure.model_dump(mode="json")
                return FastMCPResult(
                    content=[TextContent(type="text", text=json.dumps(data))],
                    structured_content=data,
                    is_error=True,
                )

    server = FastMCP("fake-hevy", middleware=[FakeBoundary()], mask_error_details=True)

    @server.tool
    async def fake() -> dict:
        try:
            raise RuntimeError("secret-in-upstream-exception")
        except RuntimeError as exc:
            raise HevyToolError(payload) from exc

    result = await server.call_tool("fake", {})
    wire = result.to_mcp_result()
    assert wire.is_error is True
    assert wire.structured_content == payload.model_dump(mode="json")
    assert json.loads(wire.content[0].text) == wire.structured_content
    assert "secret-in-upstream-exception" not in str(wire)
    assert str(HevyToolError(payload)) == "Hevy operation failed"


def test_json_stderr_discards_external_messages_args_and_tracebacks(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    external = logging.getLogger("fastmcp.secret-test")
    external.handlers = [logging.StreamHandler(stream)]
    external.propagate = False
    configure_logging("DEBUG")
    try:
        raise RuntimeError("exception-secret")
    except RuntimeError:
        external.exception(
            "request %s",
            {"notes": "nested-secret", "access_token": "token-secret"},
            stack_info=True,
        )
        logging.getLogger("hevy_mcp").exception("title=private-title email=private-email")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    events = [json.loads(line) for line in lines]
    assert all(event["event"] == "external_log" for event in events)
    assert all(event["level"] == "ERROR" for event in events)
    for secret in [
        "exception-secret",
        "nested-secret",
        "token-secret",
        "private-title",
        "private-email",
        "Traceback",
        "Stack",
    ]:
        assert secret not in stream.getvalue()
    logging.getLogger().removeHandler(logging.getLogger().handlers[0])


def test_unknown_log_level_has_no_secret_in_error():
    with pytest.raises(ValueError) as exc:
        configure_logging("bad-secret-level")
    assert "bad-secret-level" not in str(exc.value)


def test_malformed_external_log_cannot_trigger_logging_traceback(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    configure_logging("DEBUG")
    logging.getLogger("httpx").error("private-secret %d", "private-secret")
    assert "private-secret" not in stream.getvalue()
    assert "Traceback" not in stream.getvalue()
    assert json.loads(stream.getvalue())["event"] == "external_log"
    logging.getLogger().removeHandler(logging.getLogger().handlers[0])


def test_partial_mutation_schema_never_allows_retryable_true():
    from hevy_mcp.results import PartialMutationFailure

    payload = {"error": {**error_data(), "retryable": True}, "outcome": outcome_data()}
    with pytest.raises(ValidationError):
        PartialMutationFailure.model_validate(payload)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, PartialMutationFailure.model_json_schema())
