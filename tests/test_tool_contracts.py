import json

import jsonschema
import pytest
from fakes import INPUT, Adapter, FakeService
from fastmcp import Client

from hevy_mcp import errors
from hevy_mcp.boundary import ERRORS
from hevy_mcp.server import TOOL_DESCRIPTIONS, create_server
from hevy_mcp.service import HevyService

READS = [
    "list_workouts",
    "get_workout",
    "get_recent_workouts",
    "get_exercise_history",
    "list_routines",
    "get_routine",
]
ARGS = {
    "get_workout": {"workout_id": "w0"},
    "get_routine": {"routine_id": "r1"},
    "get_exercise_history": {"exercise_template_id": "t1"},
}


def structured(result):
    return result.structured_content


def decoded(result):
    values = [json.loads(block.text) for block in result.content]
    if result.structured_content and "result" in result.structured_content:
        assert result.structured_content["result"] == values
        return values
    assert len(values) == 1 and values[0] == result.structured_content
    return values[0]


@pytest.mark.parametrize("name", READS)
async def test_legacy_success_values_and_output_schemas(name):
    fake = FakeService()
    server = create_server(lambda: fake)
    args = ARGS.get(name, {})
    expected = await getattr(fake.service, name)(**args)
    expected = (
        [x.model_dump(mode="json") for x in expected]
        if isinstance(expected, list)
        else expected.model_dump(mode="json")
    )
    result = await server.call_tool(name, args)
    assert not result.is_error
    assert decoded(result) == expected
    schema = next(x.output_schema for x in await server.list_tools() if x.name == name)
    jsonschema.validate(result.structured_content, schema)


@pytest.mark.parametrize("name", READS)
@pytest.mark.parametrize("format", [None, "summary", "full"])
@pytest.mark.parametrize("raw", [None, False, True])
async def test_a1_complete_raw_format_matrix(name, format, raw):
    fake = FakeService()
    server = create_server(lambda: fake)
    result = await server.call_tool(
        name, {**ARGS.get(name, {}), "format": format, "include_raw": raw}
    )
    assert not result.is_error, decoded(result)
    value = decoded(result)
    items = (
        value["items"] if name == "list_workouts" else value if isinstance(value, list) else [value]
    )
    summary = format == "summary" or (name == "list_workouts" and format != "full")
    detail = name in {"get_workout", "get_routine"}
    expected_raw = (
        raw if raw is not None else not summary if format is None else detail and format == "full"
    )
    for item in items:
        assert ("raw" in item) == expected_raw
        if summary:
            assert "sets" not in item and "exercises" not in item
        elif name != "get_exercise_history":
            assert item["exercises"][0]["extra_exercise"] == 9
            assert item["exercises"][0]["sets"][0]["extra_set"] == 8
    schema = next(x.output_schema for x in await server.list_tools() if x.name == name)
    jsonschema.validate(result.structured_content, schema)
    if name == "list_workouts":
        assert sum(call[0] == "get_workout" for call in fake.adapter.calls) == (
            10 if format == "full" or raw is True else 0
        )


async def test_list_hydration_fails_whole_call_and_never_drops_rows():
    fake = FakeService()
    original = fake.service.get_workout

    async def fail(workout_id):
        if workout_id == "w4":
            raise errors.NotFoundError("private upstream text")
        return await original(workout_id)

    fake.service.get_workout = fail
    result = await create_server(lambda: fake).call_tool(
        "list_workouts", {"format": "full", "include_meta": True}
    )
    assert result.is_error and decoded(result)["error"]["code"] == "NOT_FOUND"
    assert "next_cursor" not in decoded(result)
    assert "private upstream" not in str(result)


@pytest.mark.parametrize("exception,code,retryable,hint", ERRORS, ids=[x[1] for x in ERRORS])
@pytest.mark.parametrize("mutation", [False, True])
async def test_every_error_maps_stably_with_safe_hints_and_schemas(
    exception, code, retryable, hint, mutation
):
    adapter = Adapter()
    adapter.failure = exception("private note Bearer very-secret")
    server = create_server(lambda: HevyService(adapter))
    name = "update_routine" if mutation else "get_workout"
    args = {"routine_id": "r1", "routine": INPUT.model_dump()} if mutation else {"workout_id": "w0"}
    async with Client(server) as client:
        result = await client.call_tool(name, args, raise_on_error=False)
    assert result.is_error
    value = result.structured_content
    assert value["error"]["code"] == code
    assert value["error"]["retryable"] is (retryable and not mutation)
    assert value["error"]["hint"] == hint
    assert "very-secret" not in str(result) and "private note" not in str(result)
    schema = next(x.output_schema for x in await server.list_tools() if x.name == name)
    jsonschema.validate(value, schema)


@pytest.mark.parametrize(
    "args",
    [
        {"page": 0},
        {"page_size": 51},
        {"page_size": None},
        {"typo": 1},
        {"format": "compact"},
        {"scan_limit": 201},
    ],
)
async def test_argument_validation_has_typed_error_and_zero_io(args):
    fake = FakeService()
    result = await create_server(lambda: fake).call_tool("list_workouts", args)
    assert result.is_error and decoded(result)["error"]["code"] == "INVALID_ARGUMENT"
    assert not fake.adapter.calls and not fake.batches


async def test_output_body_failure_is_schema_and_unexpected_exception_is_internal():
    fake = FakeService()

    async def malformed(*args):
        from hevy_mcp.models import WorkoutPage

        return WorkoutPage.model_construct(items=["broken"], page=1, page_count=1)

    # An unexpected implementation defect is INTERNAL_ERROR, never raw output.
    fake.service.list_workouts = malformed
    result = await create_server(lambda: fake).call_tool("list_workouts", {})
    assert result.is_error and decoded(result)["error"]["code"] == "INTERNAL_ERROR"
    fake.adapter.failure = errors.SchemaError("bad shape")
    result = await create_server(lambda: fake).call_tool("get_workout", {"workout_id": "w0"})
    assert decoded(result)["error"]["code"] == "SCHEMA_ERROR"


async def test_unconfigured_delete_precedes_service_runtimeerror():
    fake = FakeService()
    result = await create_server(lambda: fake).call_tool(
        "delete_workout", {"workout_id": "w0", "expected_title": "x", "expected_started_at": "x"}
    )
    assert result.is_error
    value = decoded(result)
    assert value["error"]["code"] == "CONFIGURATION_ERROR"
    assert value["outcome"]["request_state"] == "not_attempted"
    assert value["outcome"]["mutation_attempts"] == 0 and not fake.adapter.calls


async def test_schema_inventory_and_recipe_content():
    tools = {x.name: x for x in await create_server(lambda: None, profile="parity").list_tools()}
    assert len(tools) == 10
    for name, tool in tools.items():
        assert "ctx" not in tool.parameters["properties"]
        assert tool.output_schema["type"] == "object"
        assert tool.parameters["additionalProperties"] is False
        alias = tool.annotations.model_dump(by_alias=True)
        expected = (
            (True, False, True, False)
            if name in READS
            else (False, name.startswith("delete_"), False, False)
        )
        assert (
            tuple(
                alias[k]
                for k in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
            )
            == expected
        )
    assert tools["list_workouts"].parameters["properties"]["page_size"]["default"] == 10
    assert tools["get_exercise_history"].parameters["properties"]["page_size"]["default"] == 20
    for name, words in {
        "list_workouts": ["50", "include_meta", "next_cursor", "get_workout"],
        "get_workout": ["title", "start time", "delete_workout", "include_raw=false"],
        "get_recent_workouts": ["10", "100", "summary", "client-side"],
        "get_exercise_history": ["progression", "include_stats=true", "all-time"],
        "update_routine": ["full replacement", "get_routine", "one write attempt", "§4.3"],
        "delete_workout": [
            "explicitly requested",
            "duplicated",
            "snapshot",
            "absence",
            "configured backend",
        ],
    }.items():
        assert all(word in TOOL_DESCRIPTIONS[name] for word in words)
