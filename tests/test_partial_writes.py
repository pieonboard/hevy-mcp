import json

import pytest
from fakes import INPUT, Adapter, routine, workout
from test_tool_contracts import decoded

from hevy_mcp import errors
from hevy_mcp.deletion import DeletionOrchestrator
from hevy_mcp.models import parse_workout
from hevy_mcp.server import create_server
from hevy_mcp.service import HevyService


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
@pytest.mark.parametrize(
    "failure",
    [
        errors.NotFoundError,
        errors.AuthenticationError,
        errors.SchemaError,
        errors.IdentityMismatchError,
    ],
)
async def test_post_acknowledged_routine_failure_preserves_target_and_no_replay(operation, failure):
    class Broken(Adapter):
        async def call(self, name, *args, **kwargs):
            if name == "get_routine":
                self.calls.append((name, args, kwargs))
                # NotFound on delete is the authoritative successful absence case.
                if operation == "delete" and failure is errors.NotFoundError:
                    raise failure("private body")
                if failure is errors.IdentityMismatchError:
                    value = routine()
                    value["routine"]["id"] = "different"
                    return value
                raise failure("private body")
            return await super().call(name, *args, **kwargs)

    adapter = Broken()
    server = create_server(lambda: HevyService(adapter))
    args = {"routine": INPUT.model_dump()} if operation == "create" else {"routine_id": "r1"}
    if operation == "update":
        args["routine"] = INPUT.model_dump()
    result = await server.call_tool(f"{operation}_routine", args)
    value = decoded(result)
    if operation == "delete" and failure is errors.NotFoundError:
        assert not result.is_error and value == {"deleted": True, "routine_id": "r1"}
    else:
        assert result.is_error
        outcome = value["outcome"]
        assert outcome["resource_id"] == "r1" and outcome["request_state"] == "acknowledged"
        assert outcome["phase"] == "verification" and outcome["mutation_attempts"] == 1
        assert outcome["automatic_replay"] is False and value["error"]["retryable"] is False
        if operation == "delete" and failure is errors.IdentityMismatchError:
            assert outcome["effect"] == "still_present"
        else:
            assert outcome["verification"] == (
                "mismatch" if failure is errors.IdentityMismatchError else "inconclusive"
            )
    assert sum(x[0] == f"{operation}_routine" for x in adapter.calls) == 1
    assert sum(x[0] == "get_routine" for x in adapter.calls) == 1


@pytest.mark.parametrize("response", [{"routineId": None}, {}, {"routineId": ""}])
async def test_malformed_create_response_keeps_unknown_identity(response):
    class Broken(Adapter):
        async def call(self, name, *args, **kwargs):
            self.calls.append((name, args, kwargs))
            return response

    adapter = Broken()
    result = await create_server(lambda: HevyService(adapter)).call_tool(
        "create_routine", {"routine": INPUT.model_dump()}
    )
    value = decoded(result)
    assert result.is_error and value["error"]["code"] == "SCHEMA_ERROR"
    assert value["outcome"]["resource_id"] is None
    assert value["outcome"]["request_state"] == "acknowledged"
    assert value["outcome"]["effect"] == "unknown"
    assert len(adapter.calls) == 1


@pytest.mark.parametrize(
    "phase", ["preflight", "identity", "snapshot", "attempt", "present", "inconclusive", "absent"]
)
async def test_workout_delete_state_machine_observation_retains_snapshot_backend(tmp_path, phase):
    calls = []
    target = parse_workout(workout(0))

    class Client:
        async def get_workout(self, workout_id):
            calls.append("get")
            if len(calls) == 1:
                if phase == "preflight":
                    raise errors.NotFoundError("private preflight")
                return target
            if phase == "inconclusive":
                raise errors.SchemaError("private verification")
            if phase == "absent":
                raise errors.NotFoundError("gone")
            return target

        async def delete(self, path):
            calls.append("delete")
            if phase == "attempt":
                raise errors.BackendDriftError("private attempt")

    class Snapshot:
        def save(self, value, backend):
            calls.append("snapshot")
            if phase == "snapshot":
                raise errors.SnapshotError("private disk error")
            path = tmp_path / "saved.json"
            path.write_text(json.dumps(value.model_dump(mode="json")))
            return path

    service = HevyService(Adapter(), DeletionOrchestrator(Client(), Snapshot(), "internal"))
    result = await create_server(lambda: service).call_tool(
        "delete_workout",
        {
            "workout_id": "w0",
            "expected_title": "wrong" if phase == "identity" else target.title,
            "expected_started_at": target.started_at.isoformat(),
        },
    )
    value = decoded(result)
    if phase == "absent":
        assert not result.is_error and value["verified_absent"]
        assert value["snapshot_path"] == str(tmp_path / "saved.json")
    else:
        assert result.is_error
        state = value["outcome"]
        assert state["resource_id"] == "w0"
        if phase in {"attempt", "present", "inconclusive"}:
            assert (
                state["snapshot_path"] == str(tmp_path / "saved.json")
                and state["backend"] == "internal"
            )
            assert state["mutation_attempts"] == 1
            if phase == "present":
                assert state["verification"] == "present" and state["effect"] == "still_present"
            if phase == "inconclusive":
                assert (
                    state["cause_code"] == "SCHEMA_ERROR"
                    and state["verification"] == "inconclusive"
                )
        else:
            assert state["mutation_attempts"] == 0 and state["request_state"] == "not_attempted"
            assert state["snapshot_path"] is None
    assert calls.count("delete") == int(phase in {"attempt", "present", "inconclusive", "absent"})
    if phase == "attempt":
        assert calls == ["get", "snapshot", "delete"]  # No invented reconciliation.
