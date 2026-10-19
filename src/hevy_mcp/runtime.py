"""Shared account lane, cancellation drain and non-invasive mutation observation."""

# Runtime additions leave the historical service and CLI protocols above intact.
import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import ValidationError

from .boundary import classify, failure
from .errors import ConfigurationError, EndpointChangedError, IdentityMismatchError, NotFoundError
from .results import (
    ErrorContext,
    HevyToolError,
    MutationErrorView,
    MutationOutcome,
    PartialMutationFailure,
    Phase,
)
from .service import HevyService, PersistentUnofficialAdapter


class UnavailableOperation(ConfigurationError):
    """An explicitly gated operation has no implementation in this build."""


class DeleteResult(Protocol):
    verified_absent: bool

    def model_dump(self, *, mode: Literal["json"]) -> dict[str, Any]: ...


class DeleteTester(Protocol):
    async def delete(
        self, workout_id: str, expected_title: str, expected_started_at: str
    ) -> DeleteResult: ...


@dataclass
class Observation:
    outcome: MutationOutcome | None
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    cancelled: bool = False
    guard: threading.Lock = field(default_factory=threading.Lock)

    def mark(self, **changes: Any) -> None:
        with self.guard:
            if self.outcome:
                self.outcome = self.outcome.model_copy(update=changes)


current: ContextVar[Observation | None] = ContextVar("hevy_operation", default=None)


def mark(**changes: Any) -> None:
    if recorder := current.get():
        recorder.mark(**changes)


class SyncRoutines:
    """Observe actual synchronous method entry, including inside to_thread."""

    def __init__(self, raw: Any):
        self.raw = raw

    def __getattr__(self, name: str) -> Any:
        original = getattr(self.raw, name)
        if name not in {"create", "update", "delete"}:
            return original

        def invoke(*args: Any, **kwargs: Any) -> Any:
            mark(
                phase="mutation",
                request_state="may_have_been_sent",
                effect="unknown",
                mutation_attempts=1,
            )
            result = original(*args, **kwargs)
            mark(phase="response", request_state="acknowledged")
            return result

        return invoke


class ObservedSDK:
    def __init__(self, raw: Any):
        self.raw = raw
        self.routines = SyncRoutines(raw.routines)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)


class ObservedAdapter:
    def __init__(self, raw: Any):
        self.precise = isinstance(raw, PersistentUnofficialAdapter)
        if self.precise:
            factory = raw.client_factory
            self.raw = PersistentUnofficialAdapter(
                raw.sessions, lambda session: ObservedSDK(factory(session)), raw.sleeper
            )
        else:
            self.raw = raw

    async def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        mutation = name in {"create_routine", "update_routine", "delete_routine"}
        if mutation:
            mark(phase="session")
            if not self.precise:
                mark(
                    phase="mutation",
                    request_state="may_have_been_sent",
                    effect="unknown",
                    mutation_attempts=1,
                )
        elif name == "get_routine" and (recorder := current.get()) and recorder.outcome:
            mark(phase="verification", resource_id=args[0])
        try:
            result = await self.raw.call(name, *args, **kwargs)
        except Exception as exc:
            if not mutation:
                mark(cause_code=classify(exc))
            raise
        if mutation:
            changes: dict[str, Any] = {"phase": "response", "request_state": "acknowledged"}
            if name == "create_routine" and isinstance(result, dict):
                from .models import parse_routine

                candidate = result.get("routineId")
                if isinstance(candidate, str) and candidate.strip():
                    changes["resource_id"] = candidate
                elif "routineId" not in result:
                    try:
                        changes["resource_id"] = parse_routine(result).id
                    except Exception:
                        pass
            mark(**changes)
        return result


class ObservedSnapshot:
    def __init__(self, raw: Any):
        self.raw = raw

    def save(self, workout: Any, backend: str) -> Any:
        mark(phase="snapshot", backend=backend)
        path = self.raw.save(workout, backend)
        mark(snapshot_path=str(path))
        return path


class ObservedRaw:
    def __init__(self, raw: Any):
        self.raw = raw

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        mark(
            phase="mutation",
            request_state="may_have_been_sent",
            effect="unknown",
            mutation_attempts=1,
        )
        result = await self.raw.request(*args, **kwargs)
        mark(phase="response", request_state="acknowledged")
        return result

    async def close(self) -> None:
        await self.raw.close()


class ObservedDelete:
    def __init__(self, raw: Any):
        from .delete_client import SessionDeleteClient

        self.precise = isinstance(raw, SessionDeleteClient)
        if self.precise:
            # Only the client creation delegate changes; request policy stays copied.
            class Client(SessionDeleteClient):
                async def _client(self) -> Any:
                    return ObservedRaw(await raw._client())

            self.raw: Any = Client(raw.sessions, raw.client_factory)
        else:
            self.raw = raw

    async def get_workout(self, workout_id: str) -> Any:
        recorder = current.get()
        verification = bool(recorder and recorder.outcome and recorder.outcome.mutation_attempts)
        mark(phase="verification" if verification else "preflight")
        try:
            result = await self.raw.get_workout(workout_id)
        except NotFoundError:
            if verification:
                mark(verification="absent", effect="applied")
            raise
        except Exception as exc:
            if verification:
                mark(verification="inconclusive", cause_code=classify(exc))
            raise
        if verification:
            mark(verification="present", effect="still_present")
        return result

    async def delete(self, path: str) -> Any:
        if not self.precise:
            mark(
                phase="mutation",
                request_state="may_have_been_sent",
                effect="unknown",
                mutation_attempts=1,
            )
        result = await self.raw.delete(path)
        mark(phase="response", request_state="acknowledged")
        return result


def observed_service(service: Any) -> Any:
    if not isinstance(service, HevyService):
        return service
    from .deletion import DeletionOrchestrator

    deletion = service.deletion
    if isinstance(deletion, DeletionOrchestrator):
        deletion = DeletionOrchestrator(
            ObservedDelete(deletion.client),
            ObservedSnapshot(deletion.snapshots),
            deletion.backend.name,
        )
    return HevyService(ObservedAdapter(service.adapter), deletion)


class Runtime:
    """One loop-owned lane; cancellation never cancels a dispatched account task."""

    def __init__(self, service_factory: Callable[[], Any], *, admission_timeout: float = 10):
        self.service = observed_service(service_factory())
        self.admission_timeout = admission_timeout
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lane: asyncio.Lock | None = None
        self.tasks: set[asyncio.Task[Any]] = set()
        self.closing = False

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self.loop is not None and self.loop is not loop:
            raise RuntimeError("A runtime cannot span event loops")
        if self.lane is None:
            self.loop, self.lane = loop, asyncio.Lock()
        self.closing = False

    async def close(self) -> None:
        self.closing = True
        while self.tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self.tasks)), return_exceptions=True
            )
        self.lane, self.loop = None, None

    async def invoke[T](
        self, tool: str, action: Callable[[], Awaitable[T]], *, resource_id: str | None = None
    ) -> T:
        mutation = tool.startswith(("create_", "update_", "delete_"))
        if self.closing:
            raise HevyToolError(failure("BUSY", tool=tool, mutation=mutation))
        self.start()
        outcome = (
            MutationOutcome(
                operation="create"
                if tool.startswith("create_")
                else "update"
                if tool.startswith("update_")
                else "delete",
                resource_type="workout" if tool == "delete_workout" else "routine",
                resource_id=resource_id,
                phase="validation",
                request_state="not_attempted",
                effect="not_applied",
                verification="not_run",
                snapshot_path=None,
                backend=getattr(
                    getattr(getattr(self.service, "deletion", None), "backend", None), "name", None
                )
                if tool == "delete_workout"
                else None,
                mutation_attempts=0,
            )
            if mutation
            else None
        )
        observation = Observation(outcome)

        async def run() -> T:
            token = current.set(observation)
            acquired = False
            try:
                assert self.lane is not None
                try:
                    await asyncio.wait_for(self.lane.acquire(), self.admission_timeout)
                    acquired = True
                except TimeoutError:
                    raise HevyToolError(failure("BUSY", tool=tool, mutation=mutation)) from None
                if observation.cancelled:
                    raise asyncio.CancelledError
                if tool == "delete_workout" and not getattr(self.service, "deletion", None):
                    raise ConfigurationError("No workout deletion backend")
                result = await action()
                if observation.outcome:
                    mark(
                        phase="verification",
                        effect="applied",
                        verification="absent" if tool.startswith("delete_") else "verified",
                    )
                return result
            except HevyToolError:
                raise
            except Exception as exc:
                code = (
                    "SCHEMA_ERROR"
                    if isinstance(exc, ValidationError)
                    else "INVALID_ARGUMENT"
                    if isinstance(exc, ValueError)
                    and (
                        observation.outcome is None
                        or observation.outcome.request_state == "not_attempted"
                    )
                    else classify(exc)
                )
                state = observation.outcome
                phase: Phase = state.phase if state else "response"
                detail = failure(
                    code,
                    mutation=mutation,
                    context=ErrorContext(
                        tool=tool,
                        phase=phase,
                        correlation_id=observation.correlation_id,
                        resource_id=state.resource_id if state else resource_id,
                    ),
                )
                if state:
                    if state.phase == "verification" and state.verification == "not_run":
                        state.verification = (
                            "mismatch"
                            if isinstance(exc, IdentityMismatchError)
                            else "present"
                            if isinstance(exc, EndpointChangedError) and tool == "delete_routine"
                            else "inconclusive"
                        )
                        state.effect = (
                            "still_present" if state.verification == "present" else "unknown"
                        )
                    state.cause_code = state.cause_code or code
                    detail = PartialMutationFailure(
                        error=MutationErrorView.model_validate(detail.error.model_dump()),
                        outcome=state,
                    )
                logging.getLogger("hevy_mcp.runtime").error(
                    "operation_failure",
                    extra={
                        "correlation_id": observation.correlation_id,
                        "code": code,
                        "tool": tool,
                        "phase": phase,
                    },
                )
                raise HevyToolError(detail) from None
            finally:
                if acquired:
                    assert self.lane is not None
                    self.lane.release()
                current.reset(token)

        task = asyncio.create_task(run())
        self.tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self.tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Consume failure even if the caller has gone away.
            if observation.cancelled:
                logging.getLogger("hevy_mcp.runtime").info(
                    "cancelled_operation_drained",
                    extra={
                        "correlation_id": observation.correlation_id,
                        "tool": tool,
                        "phase": observation.outcome.phase if observation.outcome else "response",
                        "status": observation.outcome.effect
                        if observation.outcome
                        else "read_drained",
                        "code": observation.outcome.cause_code if observation.outcome else None,
                    },
                )

        task.add_done_callback(completed)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            observation.cancelled = True
            raise
