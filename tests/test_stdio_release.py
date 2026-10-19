"""Release B4/B5 on real MCP pipes, with only a synthetic account service."""

import json
import sys
from pathlib import Path

import jsonschema
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CODE = """
from fakes import FakeService
from hevy_mcp.server import create_server
from hevy_mcp.logging import configure_logging
server = create_server(lambda: FakeService())
configure_logging('DEBUG')
server.run(transport='stdio', show_banner=False)
"""


async def test_release_b4_b5_schemas_errors_and_resources_over_stdio(tmp_path, child_env):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("child_bootstrap.py")), "code", CODE],
        cwd=tmp_path,
        env=child_env,
    )
    with (tmp_path / "stderr.log").open("w") as log:
        async with stdio_client(params, errlog=log) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                schemas = {t.name: t.output_schema for t in (await session.list_tools()).tools}
                assert len(schemas) == 12
                search = await session.call_tool(
                    "search_exercise_templates", {"query": "bench", "limit": 1}
                )
                assert not search.is_error
                jsonschema.validate(search.structured_content, schemas["search_exercise_templates"])
                template_id = search.structured_content["items"][0]["exercise_template_id"]
                resource = await session.read_resource("hevy://templates/" + template_id)
                assert (
                    json.loads(resource.contents[0].text) == search.structured_content["items"][0]
                )
                for args, code in [
                    ({"include_custom": True}, "CAPABILITY_UNAVAILABLE"),
                    ({"muscle_group": " "}, "INVALID_ARGUMENT"),
                ]:
                    result = await session.call_tool("search_exercise_templates", args)
                    assert result.is_error and result.structured_content["error"]["code"] == code
                    jsonschema.validate(
                        result.structured_content, schemas["search_exercise_templates"]
                    )
                stats = await session.call_tool(
                    "get_exercise_stats",
                    {
                        "exercise_template_id": "t1",
                        "limit": 1000,
                        "scan_limit": 3,
                        "include_raw": True,
                    },
                )
                data = stats.structured_content
                assert not stats.is_error and data["returned_count"] == 3 and data["truncated"]
                assert data["stats"]["best_e1rm"]["workout_id"] == "w2"
                assert not data["records_are_all_time"]
                assert data["history"][0]["raw"]["id"] == "w0"
                assert json.loads(stats.content[0].text) == data
                assert stats.meta["hevy"]["next_offset"] == 3
                jsonschema.validate(data, schemas["get_exercise_stats"])
                invalid = await session.call_tool(
                    "get_exercise_stats", {"exercise_template_id": "t1", "limit": 1001}
                )
                assert (
                    invalid.is_error
                    and invalid.structured_content["error"]["code"] == "INVALID_ARGUMENT"
                )
                jsonschema.validate(invalid.structured_content, schemas["get_exercise_stats"])
