from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .config import atomic_write, secure_dir
from .errors import (
    AuthenticationError,
    BackendDriftError,
    EndpointChangedError,
    IdentityMismatchError,
    MutationIndeterminateError,
    NotFoundError,
    SchemaError,
    SnapshotError,
    TransportError,
)
from .models import DeletionResult, Workout, normalize_timestamp


class DeleteClient(Protocol):
    async def get_workout(self, workout_id: str) -> Workout: ...
    async def delete(self, path: str) -> Any: ...


class WorkoutDeleteBackend:
    name: str
    prefix: str

    def path(self, workout_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", workout_id):
            raise ValueError("unsafe workout ID")
        return f"/{self.prefix}/workouts/{workout_id}"


class V1WorkoutDeleteBackend(WorkoutDeleteBackend):
    name = "v1"
    prefix = "v1"


class V2WorkoutDeleteBackend(WorkoutDeleteBackend):
    name = "v2"
    prefix = "v2"


class InternalWorkoutDeleteBackend(WorkoutDeleteBackend):
    name = "internal"
    prefix = ""

    def path(self, workout_id: str) -> str:
        validate_workout_id(workout_id)
        return f"/workout/{workout_id}"


def validate_workout_id(workout_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", workout_id):
        raise ValueError("unsafe workout ID")
    return workout_id


def backend_for(name: str) -> WorkoutDeleteBackend:
    if name == "internal":
        return InternalWorkoutDeleteBackend()
    if name == "v1":
        return V1WorkoutDeleteBackend()
    if name == "v2":
        return V2WorkoutDeleteBackend()
    raise ValueError("No proven workout delete backend configured")


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = root

    def save(self, workout: Workout, backend: str) -> Path:
        try:
            secure_dir(self.root)
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", workout.id)[:120]
            if not safe_id:
                raise ValueError("unsafe workout ID for snapshot")
            path = self.root / f"{stamp}_{safe_id}.json"
            if path.resolve().parent != self.root.resolve():
                raise ValueError("snapshot path escaped archive directory")
            payload = {
                "snapshot_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "backend": backend,
                "workout": workout.model_dump(mode="json"),
                "raw": workout.raw,
            }
            atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", exclusive=True)
            return path
        except Exception as exc:
            raise SnapshotError(f"Could not persist deletion snapshot: {exc}") from exc


class DeletionOrchestrator:
    def __init__(self, client: DeleteClient, snapshots: Any, backend: str):
        self.client = client
        self.snapshots = snapshots
        self.backend = backend_for(backend)

    async def delete(
        self, workout_id: str, expected_title: str, expected_started_at: str
    ) -> DeletionResult:
        validate_workout_id(workout_id)
        workout = await self.client.get_workout(workout_id)
        if workout.id != workout_id or workout.title != expected_title:
            raise IdentityMismatchError("workout ID/title mismatch; no DELETE sent")
        try:
            expected = normalize_timestamp(expected_started_at)
        except Exception as exc:
            raise IdentityMismatchError("invalid expected start timestamp; no DELETE sent") from exc
        if workout.started_at != expected:
            raise IdentityMismatchError("workout start timestamp mismatch; no DELETE sent")
        try:
            snapshot = self.snapshots.save(workout, self.backend.name)
        except SnapshotError:
            raise
        except Exception as exc:
            raise SnapshotError(f"Could not persist deletion snapshot: {exc}") from exc
        ambiguous_failure: Exception | None = None
        try:
            await self.client.delete(self.backend.path(workout_id))
        except BackendDriftError:
            raise
        except (AuthenticationError, EndpointChangedError, SchemaError, TransportError) as exc:
            ambiguous_failure = exc
        except Exception as exc:
            raise BackendDriftError(
                f"Configured {self.backend.name} delete backend failed; no fallback attempted"
            ) from exc
        try:
            await self.client.get_workout(workout_id)
        except NotFoundError:
            return DeletionResult(
                workout_id=workout_id,
                snapshot_path=str(snapshot),
                backend=self.backend.name,
                verified_absent=True,
            )
        except Exception as exc:
            raise MutationIndeterminateError(
                "Workout DELETE outcome is indeterminate; read-back inconclusive; "
                "DELETE was not replayed and no fallback was attempted"
            ) from exc
        if ambiguous_failure is not None:
            raise BackendDriftError(
                "Workout still exists after ambiguous DELETE failure; no fallback attempted"
            ) from ambiguous_failure
        raise BackendDriftError("DELETE returned but workout still exists; no fallback attempted")
