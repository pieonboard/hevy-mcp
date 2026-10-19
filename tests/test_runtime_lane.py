import asyncio
import threading

import pytest
from fakes import FakeService
from fastmcp import Client

from hevy_mcp.boundary import failure
from hevy_mcp.results import HevyToolError
from hevy_mcp.runtime import Runtime
from hevy_mcp.server import create_server


async def test_worker_loop_heartbeat_cancelled_caller_retains_lane_and_shutdown_drains():
    runtime = Runtime(lambda: None)
    entered, release = threading.Event(), threading.Event()
    events, loops = [], []

    def blocking():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loops.append("no_running_loop")
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            loops.append("no_policy_loop")
        entered.set()
        assert release.wait(5)
        events.append("worker_finished")

    async def first():
        await asyncio.to_thread(blocking)
        events.append("verification")

    async def second():
        events.append("second")

    task = asyncio.create_task(runtime.invoke("update_routine", first))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0)
        heartbeat = []
        for _ in range(5):
            await asyncio.sleep(0)
            heartbeat.append(True)
        assert len(heartbeat) == 5
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        next_task = asyncio.create_task(runtime.invoke("get_workout", second))
        await asyncio.sleep(0.01)
        assert not events and not next_task.done()
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.01)
        assert not closing.done()
    finally:
        release.set()
    await next_task
    await closing
    assert events == ["worker_finished", "verification", "second"]
    assert loops == ["no_running_loop", "no_policy_loop"]
    assert not runtime.tasks and runtime.lane is None


async def test_cancel_before_admission_does_no_work_and_busy_is_typed():
    runtime = Runtime(lambda: None, admission_timeout=0.03)
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    async def first():
        entered.set()
        await release.wait()

    async def rejected():
        events.append("must_not_run")

    active = asyncio.create_task(runtime.invoke("get_workout", first))
    await entered.wait()
    cancelled = asyncio.create_task(runtime.invoke("update_routine", rejected))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    with pytest.raises(HevyToolError) as info:
        await runtime.invoke("get_workout", rejected)
    assert info.value.failure.error.code == "BUSY" and info.value.failure.error.retryable
    release.set()
    await active
    await runtime.close()
    assert events == []


async def test_cancel_during_verification_keeps_lane_until_readback_finishes():
    runtime = Runtime(lambda: None)
    verifying, release = asyncio.Event(), asyncio.Event()
    events = []

    async def mutation():
        events.append("write")
        verifying.set()
        await release.wait()
        events.append("verified")

    async def other():
        events.append("next")

    task = asyncio.create_task(runtime.invoke("update_routine", mutation))
    await verifying.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    following = asyncio.create_task(runtime.invoke("list_routines", other))
    await asyncio.sleep(0.01)
    assert events == ["write"]
    release.set()
    await following
    await runtime.close()
    assert events == ["write", "verified", "next"]


async def test_tool_resource_share_complete_lane_and_repeated_lifespan():
    fake = FakeService()
    original = fake.service.get_workout
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    async def blocked(workout_id):
        events.append(workout_id)
        if workout_id == "w0":
            entered.set()
            await release.wait()
        return await original(workout_id)

    fake.service.get_workout = blocked
    constructions = []

    def factory():
        constructions.append(True)
        return fake

    server = create_server(factory)
    async with Client(server) as client:
        first = asyncio.create_task(client.call_tool("get_workout", {"workout_id": "w0"}))
        await entered.wait()
        resource = asyncio.create_task(client.read_resource("hevy://workouts/w1"))
        await asyncio.sleep(0.01)
        assert events == ["w0"]
        release.set()
        await asyncio.gather(first, resource)
    # Same server, new lifecycle, on the same loop: no stale lock/client.
    async with Client(server) as client:
        assert not (await client.call_tool("list_routines", {})).is_error
    assert len(constructions) == 1 and events == ["w0", "w1"]


async def test_teardown_and_recreation_uses_fresh_loop_owned_locks():
    for _ in range(3):
        runtime = Runtime(lambda: None)

        async def safe():
            return 1

        assert await runtime.invoke("read", safe) == 1
        lane = runtime.lane
        await runtime.close()
        runtime.start()
        assert await runtime.invoke("read", safe) == 1 and runtime.lane is not lane
        await runtime.close()


async def test_bridge_does_not_get_reclassified():
    runtime = Runtime(lambda: None)

    async def action():
        raise HevyToolError(failure("CAPABILITY_UNAVAILABLE"))

    with pytest.raises(HevyToolError) as info:
        await runtime.invoke("read", action)
    assert info.value.failure.error.code == "CAPABILITY_UNAVAILABLE"
    await runtime.close()


async def test_actual_adapter_client_is_not_closed_under_cancelled_worker():
    from types import SimpleNamespace

    from fakes import INPUT, routine

    from hevy_mcp.auth import Session, SessionManager
    from hevy_mcp.service import HevyService, PersistentUnofficialAdapter

    sessions = SessionManager()
    sessions.save(Session(access_token="synthetic", refresh_token="synthetic-refresh"))
    entered, release = threading.Event(), threading.Event()
    events = []

    def update(routine_id, payload):
        events.append("write_entered")
        entered.set()
        assert release.wait(5)
        events.append("write_finished")

    def get(routine_id):
        events.append("readback")
        return routine()

    def factory(session):
        return SimpleNamespace(
            routines=SimpleNamespace(update=update, get=get),
            close=lambda: events.append("client_closed"),
        )

    runtime = Runtime(lambda: HevyService(PersistentUnofficialAdapter(sessions, factory)))

    async def action():
        return await runtime.service.update_routine("r1", INPUT)

    caller = asyncio.create_task(runtime.invoke("update_routine", action, resource_id="r1"))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await asyncio.sleep(0.01)
        assert events == ["write_entered"]
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.01)
        assert not closing.done()
    finally:
        release.set()
    await closing
    assert events == [
        "write_entered",
        "write_finished",
        "client_closed",
        "readback",
        "client_closed",
    ]
