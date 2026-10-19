"""FastMCP factory, single account runtime and quiet stdio transport."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

from fastmcp import FastMCP

from .boundary import ErrorBoundary
from .logging import configure_logging
from .resources import INSTRUCTIONS, register_resources
from .runtime import Runtime
from .tools import DESTRUCTIVE as DESTRUCTIVE
from .tools import READ_ONLY as READ_ONLY
from .tools import TOOL_DESCRIPTIONS as TOOL_DESCRIPTIONS
from .tools import WRITE as WRITE
from .tools import Facade, analytics, discovery, routines, workouts

if TYPE_CHECKING:
    from .service import HevyService


def default_service() -> "HevyService":
    from .auth import SessionManager
    from .config import SettingsStore, config_dir
    from .delete_client import SessionDeleteClient
    from .deletion import DeletionOrchestrator, SnapshotStore
    from .service import HevyService, PersistentUnofficialAdapter

    sessions = SessionManager()
    settings = SettingsStore().load()
    deletion = None
    if settings.workout_delete_backend:
        deletion = DeletionOrchestrator(
            SessionDeleteClient(sessions),
            SnapshotStore(config_dir() / "deleted-workouts"),
            settings.workout_delete_backend,
        )
    return HevyService(PersistentUnofficialAdapter(sessions), deletion)


def create_server(
    service_factory: Callable[[], object] = default_service,
    *,
    profile: Literal["release", "parity"] = "release",
) -> FastMCP:
    if profile not in {"release", "parity"}:
        raise ValueError("unknown server profile")
    runtime = Runtime(service_factory)

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Runtime]]:
        runtime.start()
        try:
            yield {"runtime": runtime}
        finally:
            await runtime.close()

    server = FastMCP(
        "hevy-mcp", instructions=INSTRUCTIONS, lifespan=lifespan, mask_error_details=True
    )
    server.add_middleware(ErrorBoundary())
    facade = Facade(runtime)
    workouts.register_tools(server, facade)
    routines.register_tools(server, facade)
    if profile == "release":
        discovery.register_tools(server, facade)
        analytics.register_tools(server, facade)
    register_resources(server, facade)
    return server


def serve(log_level: str = "INFO") -> None:
    # Framework import/registration can create handlers; consolidate afterward.
    server = create_server()
    configure_logging(log_level)
    server.run(transport="stdio", show_banner=False)
