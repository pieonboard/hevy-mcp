import json
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import protocol_exchange
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from typer.testing import CliRunner

from hevy_mcp.auth import Session, SessionManager
from hevy_mcp.cli import app, make_delete_orchestrator
from hevy_mcp.models import DeletionResult
from hevy_mcp.server import TOOL_DESCRIPTIONS, create_server


def test_tool_descriptions_cover_safety_and_semantics() -> None:
    assert "explicitly requested" in TOOL_DESCRIPTIONS["delete_workout"]
    assert "full replacement" in TOOL_DESCRIPTIONS["update_routine"]
    assert "progression" in TOOL_DESCRIPTIONS["get_exercise_history"]


async def test_server_registers_all_required_tools() -> None:
    server = create_server(service_factory=lambda: None, profile="parity")  # type: ignore[return-value]
    tools = {tool.name: tool for tool in await server.list_tools()}
    names = set(tools)
    assert {
        "list_workouts",
        "get_workout",
        "get_recent_workouts",
        "get_exercise_history",
        "list_routines",
        "get_routine",
        "create_routine",
        "update_routine",
        "delete_routine",
        "delete_workout",
    } == names
    for name in {
        "list_workouts",
        "get_workout",
        "get_recent_workouts",
        "get_exercise_history",
        "list_routines",
        "get_routine",
    }:
        assert tools[name].annotations.read_only_hint is True
        assert not tools[name].annotations.destructive_hint
    assert tools["delete_workout"].annotations.destructive_hint is True
    assert tools["delete_workout"].annotations.idempotent_hint is False
    assert tools["delete_routine"].annotations.destructive_hint is True
    for name in {"create_routine", "update_routine"}:
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is False
        assert tools[name].annotations.idempotent_hint is False


async def test_generated_schema_exposes_history_and_strict_routine_constraints() -> None:
    tools = {
        tool.name: tool for tool in await create_server(service_factory=lambda: None).list_tools()
    }
    history = tools["get_exercise_history"].parameters["properties"]
    assert history["page"]["minimum"] == 1
    assert history["page_size"]["minimum"] == 1 and history["page_size"]["maximum"] == 100
    routine = tools["create_routine"].parameters["properties"]["routine"]["properties"][
        "exercises"
    ]["items"]["properties"]["sets"]["items"]
    assert routine["additionalProperties"] is False
    assert routine["properties"]["weight_kg"]["anyOf"][0]["minimum"] == 0
    workouts = tools["list_workouts"].parameters["properties"]
    assert workouts["page"]["minimum"] == 1
    recent = tools["get_recent_workouts"].parameters["properties"]["limit"]
    assert recent["minimum"] == 1 and recent["maximum"] == 100


def test_delete_backend_tester_uses_candidate_even_when_config_unset(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HEVY_CONFIG_DIR", str(tmp_path))
    orchestrator = make_delete_orchestrator("v2")
    assert orchestrator.backend.name == "v2"


def test_cli_help_and_offline_status_do_not_leak(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HEVY_CONFIG_DIR", str(tmp_path))
    runner = CliRunner()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 1 and "not authenticated" in (result.stdout + result.stderr).lower()
    assert runner.invoke(app, ["doctor"]).exit_code == 1
    assert "--live" in runner.invoke(app, ["doctor", "--help"]).stdout


def test_doctor_live_performs_only_one_harmless_read(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HEVY_CONFIG_DIR", str(tmp_path))
    SessionManager(tmp_path).save(
        Session(access_token="secret", refresh_token="refresh", user_id="u1", email="a@b")
    )
    calls = []

    class Service:
        async def get_recent_workouts(self, limit):
            calls.append(("get_recent_workouts", limit))
            return []

    monkeypatch.setattr("hevy_mcp.server.default_service", lambda: Service())
    result = CliRunner().invoke(app, ["doctor", "--live"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["live_request_performed"] is True
    assert payload["live_read_ok"] is True
    assert payload["user_id"] == "u1"
    assert calls == [("get_recent_workouts", 1)]


@pytest.mark.asyncio
async def test_stdio_initialize_and_list_tools_smoke(child_env) -> None:
    root = Path(__file__).parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            str(Path(__file__).with_name("child_bootstrap.py")),
            "module",
            "hevy_mcp.cli",
            "serve",
        ],
        env=child_env,
        cwd=root,
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.list_tools()
    # The installed CLI serves the release profile; parity inventory is tested above.
    assert {tool.name for tool in result.tools} == {
        "search_exercise_templates",
        "get_exercise_stats",
        "list_workouts",
        "get_workout",
        "get_recent_workouts",
        "get_exercise_history",
        "list_routines",
        "get_routine",
        "create_routine",
        "update_routine",
        "delete_routine",
        "delete_workout",
    }
    for tool in result.tools:
        assert tool.annotations is not None
        assert tool.annotations.open_world_hint is False
    delete_tool = next(tool for tool in result.tools if tool.name == "delete_workout")
    assert delete_tool.annotations is not None
    assert delete_tool.annotations.destructive_hint is True


def test_stdio_stdout_contains_protocol_json_only(child_env) -> None:
    root = Path(__file__).parents[1]
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stdio-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    # Keep stdin open until replies arrive: EOF initiates FastMCP shutdown.
    completed = protocol_exchange(
        [
            sys.executable,
            str(Path(__file__).with_name("child_bootstrap.py")),
            "module",
            "hevy_mcp.cli",
            "serve",
            "--log-level",
            "DEBUG",
        ],
        messages,
        cwd=root,
        env=child_env,
    )

    stdout_messages = [json.loads(line) for line in completed.stdout.splitlines() if line]
    assert {message.get("id") for message in stdout_messages} == {1, 2}
    assert all(message.get("jsonrpc") == "2.0" for message in stdout_messages)
    assert "DEBUG:" not in completed.stdout
    assert any(json.loads(line)["level"] == "DEBUG" for line in completed.stderr.splitlines())


def test_delete_backend_help_lists_all_candidates() -> None:
    result = CliRunner().invoke(app, ["test-delete-backend", "--help"])
    assert result.exit_code == 0
    for candidate in ("internal", "v1", "v2"):
        assert candidate in result.stdout


def test_readme_documents_all_candidates_and_working_dependency_audit() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    assert "probe all candidates" in readme
    assert "uv export --frozen --no-dev --no-hashes --format requirements-txt" in readme
    assert "uvx pip-audit -r" in readme
    assert "pip-audit --locked" not in readme


def test_invalid_delete_backend_fails_before_orchestrator(monkeypatch) -> None:
    built = []
    monkeypatch.setattr(
        "hevy_mcp.cli.make_delete_orchestrator", lambda backend: built.append(backend)
    )
    result = CliRunner().invoke(
        app,
        [
            "test-delete-backend",
            "--backend",
            "other",
            "--workout-id",
            "w1",
            "--title",
            "Disposable Delete Test nonce",
            "--started-at",
            "2026-01-01T00:00:00Z",
        ],
    )
    assert result.exit_code != 0
    assert "internal, v1, or v2" in (result.stdout + result.stderr)
    assert built == []


def test_delete_backend_discovery_does_not_persist_unverified_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HEVY_CONFIG_DIR", str(tmp_path))

    class Unverified:
        async def delete(self, workout_id, title, started_at):
            return DeletionResult(
                workout_id=workout_id,
                snapshot_path=str(tmp_path / "snapshot.json"),
                backend="internal",
                verified_absent=False,
            )

    persisted = []
    monkeypatch.setattr("hevy_mcp.cli.make_delete_orchestrator", lambda backend: Unverified())
    monkeypatch.setattr(
        "hevy_mcp.cli.SettingsStore.set_delete_backend",
        lambda self, backend: persisted.append(backend),
    )
    result = CliRunner().invoke(
        app,
        [
            "test-delete-backend",
            "--backend",
            "internal",
            "--workout-id",
            "w1",
            "--title",
            "Disposable Delete Test nonce",
            "--started-at",
            "2026-01-01T00:00:00Z",
        ],
        input="y\n",
    )
    assert result.exit_code == 1
    assert persisted == []


@pytest.fixture(autouse=True)
def mock_doctor_browser(monkeypatch):
    from types import SimpleNamespace

    original = subprocess.run

    def run(command, **kwargs):
        if command[1:4] == ["-m", "playwright", "install"]:
            assert command[4:] == ["--dry-run", "chromium"]
            return SimpleNamespace(stdout="Install location: /nonexistent/chromium")
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
