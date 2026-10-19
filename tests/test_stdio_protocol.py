"""Real MCP pipes with a fake service and an active child socket guard."""

import json
import sys
from pathlib import Path

import jsonschema
import pytest
from fakes import protocol_exchange
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from hevy_mcp.boundary import ERRORS

BOOTSTRAP = str(Path(__file__).with_name("child_bootstrap.py"))
CODE = """
import logging
import socket
import warnings
from fakes import FakeService, workout
from hevy_mcp import errors
from hevy_mcp.logging import configure_logging
from hevy_mcp.server import create_server
from hevy_mcp.boundary import ERRORS
from hevy_mcp.models import parse_workout
from hevy_mcp.service import HevyService
from hevy_mcp.deletion import DeletionOrchestrator, SnapshotStore
from hevy_mcp.runtime import observed_service
from pathlib import Path
import os

# Prove the child guard is active before server construction.
try:
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(('127.0.0.1', 1))
except Exception as exc:
    assert type(exc).__name__ in ('SocketBlockedError', 'SocketConnectBlockedError')
else:
    raise AssertionError('child network guard missing')
fake = FakeService()
class PartialAdapter:
    async def call(self, name, *args, **kwargs):
        if name == 'get_routine':
            raise errors.SchemaError('private-routine-readback')
        return await fake.adapter.call(name, *args, **kwargs)
class DeleteClient:
    gets = 0
    async def get_workout(self, workout_id):
        self.gets += 1
        if self.gets > 1:
            raise errors.SchemaError('private-workout-readback')
        return parse_workout(workout(0))
    async def delete(self, path):
        return None
fake.deletion = DeletionOrchestrator(DeleteClient(), SnapshotStore(Path(os.environ['HEVY_CONFIG_DIR']) / 'deleted'), 'internal')
fake.service = observed_service(HevyService(PartialAdapter(), fake.deletion))
original = fake.service.get_workout
async def get_workout(workout_id):
    for cls, code, _, _ in ERRORS:
        if workout_id == code:
            raise cls('private-upstream-body authorization: Bearer secret-token')
    if workout_id == 'INTERNAL':
        raise RuntimeError('private-unexpected-exception')
    return await original(workout_id)
fake.service.get_workout = get_workout
server = create_server(lambda: fake, profile="parity")
configure_logging('DEBUG')
try:
    raise RuntimeError('private-external-exception')
except RuntimeError:
    logging.getLogger('httpx').exception('private-external-message %s', {'notes':'private-note','access_token':'secret-token'}, stack_info=True)
server.run(transport='stdio', show_banner=False)
"""


async def test_stdio_all_error_codes_flags_hints_and_tool_resource_schemas(child_env, tmp_path):
    log = tmp_path / "stderr.log"
    params = StdioServerParameters(
        command=sys.executable, args=[BOOTSTRAP, "code", CODE], env=child_env, cwd=tmp_path
    )
    with log.open("w") as stderr:
        async with stdio_client(params, errlog=stderr) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                inventory = await session.list_tools()
                schemas = {t.name: t.output_schema for t in inventory.tools}
                assert len(schemas) == 10
                for _, code, retryable, hint in ERRORS:
                    result = await session.call_tool("get_workout", {"workout_id": code})
                    assert result.is_error
                    value = result.structured_content
                    assert value["error"]["code"] == code
                    assert value["error"]["retryable"] is retryable
                    assert value["error"]["hint"] == hint
                    assert json.loads(result.content[0].text) == value
                    jsonschema.validate(value, schemas["get_workout"])
                invalid = await session.call_tool("list_workouts", {"page_size": 51})
                assert (
                    invalid.is_error
                    and invalid.structured_content["error"]["code"] == "INVALID_ARGUMENT"
                )
                unexpected = await session.call_tool("get_workout", {"workout_id": "INTERNAL"})
                assert (
                    unexpected.is_error
                    and unexpected.structured_content["error"]["code"] == "INTERNAL_ERROR"
                )
                partial = await session.call_tool(
                    "delete_workout",
                    {
                        "workout_id": "w0",
                        "expected_title": "Workout 0",
                        "expected_started_at": "2026-01-01T00:00:00Z",
                    },
                )
                assert (
                    partial.is_error
                    and partial.structured_content["outcome"]["mutation_attempts"] == 1
                )
                assert partial.structured_content["error"]["retryable"] is False
                jsonschema.validate(partial.structured_content, schemas["delete_workout"])
                state = partial.structured_content["outcome"]
                assert state["resource_id"] == "w0" and state["backend"] == "internal"
                assert (
                    state["verification"] == "inconclusive"
                    and state["cause_code"] == "SCHEMA_ERROR"
                )
                assert Path(state["snapshot_path"]).is_file()
                created = await session.call_tool(
                    "create_routine", {"routine": {"title": "R", "exercises": []}}
                )
                assert (
                    created.is_error
                    and created.structured_content["outcome"]["resource_id"] == "r1"
                )
                assert created.structured_content["outcome"]["request_state"] == "acknowledged"
                jsonschema.validate(created.structured_content, schemas["create_routine"])

                empty = await session.call_tool("get_recent_workouts", {"since": "2030-01-01"})
                assert (
                    not empty.is_error
                    and empty.content == []
                    and empty.structured_content == {"result": []}
                )
                assert empty.meta["hevy"]["window_applied"] == "client"
                jsonschema.validate(empty.structured_content, schemas["get_recent_workouts"])
                page = await session.call_tool(
                    "list_workouts", {"include_meta": True, "scan_limit": 3}
                )
                assert not page.is_error
                jsonschema.validate(page.structured_content, schemas["list_workouts"])
                assert len((await session.list_resources()).resources) == 4
                assert len((await session.list_resource_templates()).resource_templates) == 5
                for uri, code in [
                    ("hevy://templates/t1", "NOT_FOUND"),
                    ("hevy://workout-pages/0/5", "INVALID_ARGUMENT"),
                    ("hevy://workouts/NOT_FOUND", "NOT_FOUND"),
                    ("hevy://workouts/a%2Fb", "INVALID_ARGUMENT"),
                ]:
                    resource = await session.read_resource(uri)
                    assert resource.contents[0].mime_type == "application/json"
                    assert json.loads(resource.contents[0].text)["error"]["code"] == code
                    assert resource.meta["hevy"]["status"] == "error"
                resource = await session.read_resource("hevy://workouts/w0")
                value = json.loads(resource.contents[0].text)
                tool = await session.call_tool(
                    "get_workout", {"workout_id": "w0", "format": "full", "include_raw": False}
                )
                assert value == tool.structured_content
                continuation = await session.read_resource(
                    "hevy://workout-continuations/" + page.structured_content["next_cursor"]
                )
                assert json.loads(continuation.contents[0].text)["total_seen"] == 12
    text = log.read_text()
    assert "private-" not in text and "secret-token" not in text
    assert "Traceback" not in text and "Stack" not in text
    assert any(json.loads(line)["level"] == "DEBUG" for line in text.splitlines())


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_actual_cli_stdout_is_only_protocol_and_stderr_is_safe_json(child_env, tmp_path, level):
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "offline", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
    ]
    completed = protocol_exchange(
        [sys.executable, BOOTSTRAP, "module", "hevy_mcp.cli", "serve", "--log-level", level],
        messages,
        cwd=tmp_path,
        env=child_env,
    )
    output = [json.loads(x) for x in completed.stdout.splitlines()]
    assert {x["id"] for x in output} == {1, 2, 3}
    assert all(x["jsonrpc"] == "2.0" for x in output)
    assert len(next(x["result"]["tools"] for x in output if x["id"] == 2)) == 12
    assert all(json.loads(x)["event"] == "external_log" for x in completed.stderr.splitlines())
