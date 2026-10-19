import json

import pytest
from fakes import FakeService
from fastmcp import Client
from test_tool_contracts import decoded

from hevy_mcp import errors
from hevy_mcp.server import create_server


def resource_json(result):
    assert len(result.contents) == 1 and result.contents[0].mime_type == "application/json"
    return json.loads(result.contents[0].content)


async def test_discovery_static_help_catalog_need_no_credentials_or_upstream():
    fake = FakeService()
    fake.adapter.failure = AssertionError("IO forbidden")
    server = create_server(lambda: fake)
    async with Client(server) as client:
        assert len(await client.list_tools()) == 12
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        assert {str(r.uri) for r in resources} == {
            "hevy://help",
            "hevy://workouts",
            "hevy://routines",
            "hevy://templates",
        }
        assert len(templates) == 5
        assert all(r.mime_type == "application/json" for r in [*resources, *templates])
    help = resource_json(await server.read_resource("hevy://help"))
    assert help["catalog_status"] == "available"
    for word in ["untrusted", "full replacement", "next_cursor", "all-time", "terminal"]:
        assert word in help["instructions"]
    catalog = resource_json(await server.read_resource("hevy://templates"))
    assert catalog["returned_count"] == 20
    assert catalog["catalog"]["source_count"] == 451
    result = await server.read_resource("hevy://templates/t1")
    assert resource_json(result)["error"]["code"] == "NOT_FOUND"
    assert result.meta["hevy"]["status"] == "error"
    assert not fake.adapter.calls and not fake.batches


@pytest.mark.parametrize(
    "uri,name,args",
    [
        ("hevy://workouts", "list_workouts", {"format": "summary", "include_meta": True}),
        (
            "hevy://workouts/w0",
            "get_workout",
            {"workout_id": "w0", "format": "full", "include_raw": False},
        ),
        (
            "hevy://workout-pages/2/5",
            "list_workouts",
            {"page": 2, "page_size": 5, "format": "summary", "include_meta": True},
        ),
        ("hevy://routines", "list_routines", {"format": "summary", "include_meta": True}),
        (
            "hevy://routines/r1",
            "get_routine",
            {"routine_id": "r1", "format": "full", "include_raw": False},
        ),
    ],
)
async def test_tool_resource_equivalence(uri, name, args):
    fake = FakeService()
    server = create_server(lambda: fake)
    resource = resource_json(await server.read_resource(uri))
    tool = decoded(await server.call_tool(name, args))
    assert resource == tool


@pytest.mark.parametrize(
    "uri",
    [
        "hevy://workouts/..",
        "hevy://workouts/a%2Fb",
        "hevy://workouts/a%252Fb",
        "hevy://workouts/a%00b",
        "hevy://workouts/a%3Fb",
        "hevy://routines/a%3Ab",
        "hevy://templates/%2e%2e",
        "hevy://workout-pages/no/5",
        "hevy://workout-pages/0/5",
        "hevy://workout-pages/1/51",
        "hevy://workout-pages/1/-1",
        "hevy://workout-continuations/bad!",
    ],
)
async def test_invalid_resource_uri_has_typed_error_before_io(uri):
    fake = FakeService()
    result = await create_server(lambda: fake).read_resource(uri)
    assert resource_json(result)["error"]["code"] == "INVALID_ARGUMENT"
    assert result.meta == {"hevy": {"status": "error"}}
    assert not fake.batches and not fake.adapter.calls


async def test_framework_decodes_once_and_no_arbitrary_uri_recovery():
    fake = FakeService()
    server = create_server(lambda: fake)
    result = await server.read_resource("hevy://workouts/%770")
    assert resource_json(result)["id"] == "w0"
    assert fake.adapter.calls[0][1] == ("w0",)


async def test_continuation_resource_recovers_validated_scope():
    fake = FakeService()
    server = create_server(lambda: fake)
    first = decoded(
        await server.call_tool(
            "list_workouts",
            {"page_size": 6, "include_meta": True, "scan_limit": 3, "since": "2026-01-02"},
        )
    )
    result = resource_json(
        await server.read_resource("hevy://workout-continuations/" + first["next_cursor"])
    )
    assert [x["id"] for x in result["items"]] == [f"w{i}" for i in range(3, 9)]
    assert result["total_seen"] == 9 and result["since"] == "2026-01-02T00:00:00Z"
    recent = await server.call_tool("get_recent_workouts", {"include_meta": True, "scan_limit": 3})
    invalid = await server.read_resource(
        "hevy://workout-continuations/" + decoded(recent)["next_cursor"]
    )
    assert resource_json(invalid)["error"]["code"] == "INVALID_ARGUMENT"


async def test_domain_resource_failures_are_json_not_success_data():
    fake = FakeService()
    fake.adapter.failure = errors.NotFoundError("private upstream body")
    result = await create_server(lambda: fake).read_resource("hevy://workouts/missing")
    assert resource_json(result)["error"]["code"] == "NOT_FOUND"
    assert result.meta["hevy"]["status"] == "error" and "private upstream" not in str(result)
