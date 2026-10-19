"""Only injected offline clients; the live smoke entry is never launched."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeService
from fastmcp import Client

from hevy_mcp import errors
from hevy_mcp.server import create_server

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/smoke_test.py"
spec = importlib.util.spec_from_file_location("hevy_safe_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class RecordingClient:
    def __init__(self, client):
        self.client = client
        self.calls = []
        self.resources = []
        self.inventory = None

    async def list_tools(self):
        return self.inventory if self.inventory is not None else await self.client.list_tools()

    async def call_tool(self, name, arguments, **options):
        assert name in smoke.SAFE and name not in self.calls
        self.calls.append(name)
        return await self.client.call_tool(name, arguments, **options)

    async def read_resource(self, uri):
        assert uri in ("hevy://help", "hevy://templates") and uri not in self.resources
        self.resources.append(uri)
        return await self.client.read_resource(uri)


async def test_scaffold_invokes_each_safe_read_once_serially_with_pacing(capsys):
    fake = FakeService()
    waits = []

    async def pause(seconds):
        waits.append(seconds)

    async with Client(create_server(lambda: fake)) as client:
        recording = RecordingClient(client)
        code = await smoke.run(recording, smoke.parser().parse_args([]), sleep=pause)
    assert code == 2  # A one-observation stats scope is deliberately incomplete.
    assert set(recording.calls) == smoke.SAFE and len(recording.calls) == 8
    assert not set(recording.calls) & smoke.WRITES
    assert len(waits) == 10 and set(waits) == {2.0}
    assert len(recording.resources) == 2
    output = capsys.readouterr().out
    assert "DEGRADED get_exercise_stats" in output
    assert "Workout 0" not in output and "w0" not in output and "s1" not in output


@pytest.mark.parametrize("public_only", [False, True])
async def test_missing_login_or_public_only_is_explicit_skip(public_only, capsys):
    fake = FakeService()
    fake.adapter.failure = errors.AuthenticationError("private cookie")

    async def no_wait(seconds):
        pass

    args = smoke.parser().parse_args(["--public-only"] if public_only else [])
    async with Client(create_server(lambda: fake)) as client:
        recording = RecordingClient(client)
        code = await smoke.run(recording, args, sleep=no_wait)
    assert code == 2
    assert recording.calls[0] == "search_exercise_templates"
    assert not set(recording.calls) & smoke.WRITES
    if public_only:
        assert recording.calls == ["search_exercise_templates"]
        assert not fake.adapter.calls and not fake.batches
    assert "SKIP" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["unknown", "missing", "annotation", "schema"])
async def test_unreviewed_inventory_fails_before_any_call(change):
    async with Client(create_server(lambda: None)) as client:
        recording = RecordingClient(client)
        tools = await client.list_tools()
        if change == "unknown":
            tools.append(SimpleNamespace(name="new_remote_read"))
        elif change == "missing":
            tools.pop()
        else:
            tool = next(t for t in tools if t.name == "list_workouts")
            if change == "annotation":
                tool.annotations.read_only_hint = False
            else:
                tool.output_schema = None
        recording.inventory = tools
        assert await smoke.run(recording, smoke.parser().parse_args([])) == 1
        assert recording.calls == recording.resources == []


def test_confirmation_decline_never_opens_transport(monkeypatch):
    monkeypatch.setattr("sys.argv", ["smoke_test.py"])
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    monkeypatch.setattr(smoke, "main_async", lambda args: pytest.fail("live entry forbidden"))
    assert smoke.main() == 2


@pytest.mark.parametrize("pace", ["0", "nan", "inf", "61"])
def test_invalid_pace_rejected_before_confirmation_or_transport(monkeypatch, pace):
    monkeypatch.setattr("sys.argv", ["smoke_test.py", "--pace", pace])
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("must validate first"))
    assert smoke.main() == 1
