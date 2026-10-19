"""Synthetic account-free source and service seams; never production package data."""

import copy

from hevy_mcp.models import RoutineInput
from hevy_mcp.service import HevyService


def workout(index=0):
    return {
        "id": f"w{index}",
        "name": f"Workout {index}",
        "start_time": f"2026-01-{index % 28 + 1:02d}T00:00:00Z",
        "exercises": [
            {
                "exercise_template_id": "t1",
                "extra_exercise": 9,
                "sets": [{"id": "s1", "weight_kg": 60, "reps": 5, "extra_set": 8}],
            }
        ],
    }


def routine():
    return {"routine": {"id": "r1", "title": "Routine", "exercises": workout()["exercises"]}}


class Adapter:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [workout(i) for i in range(12)]
        self.calls = []
        self.failure = None

    async def call(self, action, *args, **kwargs):
        self.calls.append((action, args, kwargs))
        if self.failure:
            raise self.failure
        if action == "list_workouts":
            start = (kwargs.get("page", 1) - 1) * kwargs["limit"]
            return {"workouts": copy.deepcopy(self.rows[start : start + kwargs["limit"]])}
        if action == "get_workout":
            return copy.deepcopy(next(row for row in self.rows if row["id"] == args[0]))
        if action == "exercise_history":
            start = (kwargs.get("page", 1) - 1) * kwargs["limit"]
            return copy.deepcopy(self.rows[start : start + kwargs["limit"]])
        if action == "list_routines":
            return {"updated": [routine()], "deleted": [], "isMore": False}
        if action == "get_routine":
            return routine()
        if action == "create_routine":
            return {"routineId": "r1"}
        if action in {"update_routine", "delete_routine"}:
            return None
        raise AssertionError(action)


class FakeService:
    """Delegate all normalization to the actual unchanged service."""

    def __init__(self, rows=None):
        self.adapter = Adapter(rows)
        self.service = HevyService(self.adapter)
        self.batches = []
        self.deletion = None

    def __getattr__(self, name):
        return getattr(self.service, name)

    async def read_batch(self, kind, exercise_id, offset, limit, deadline):
        self.batches.append((kind, exercise_id, offset, limit))
        assert 1 <= limit <= 5
        rows = copy.deepcopy(self.adapter.rows[offset : offset + limit])
        return rows if kind == "history" else {"workouts": rows}


INPUT = RoutineInput(title="Routine", exercises=[])


def protocol_exchange(command, messages, *, cwd, env):
    """Keep stdin open until each response arrives; EOF is a shutdown signal."""
    import json
    import selectors
    import subprocess

    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        lines = []
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                for message in messages:
                    process.stdin.write(json.dumps(message) + "\n")
                    process.stdin.flush()
                    if "id" in message:
                        if not selector.select(20):
                            raise AssertionError("MCP response timed out")
                        line = process.stdout.readline()
                        assert line, "child closed stdout before response"
                        lines.append(line)
            process.stdin.close()
            process.stdin = None
            tail, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            return subprocess.CompletedProcess(
                command, process.returncode, "".join(lines) + tail, stderr
            )
        except BaseException:
            process.kill()
            process.communicate()
            raise
