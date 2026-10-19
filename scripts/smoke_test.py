"""Opt-in, confirmed SAFE stdio reads; never run by pytest or at startup.

Exit 0: every SAFE check passed. Exit 1: failure. Exit 2: skipped/degraded.
Only codes/counts are printed. Reads may refresh and persist session tokens.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from hevy_mcp.auth import SessionManager
from hevy_mcp.catalog import TemplateSearchResult
from hevy_mcp.resources import Help
from hevy_mcp.results import Failure
from hevy_mcp.schemas import validate_output

SAFE = frozenset(
    {
        "list_workouts",
        "get_workout",
        "get_recent_workouts",
        "get_exercise_history",
        "list_routines",
        "get_routine",
        "search_exercise_templates",
        "get_exercise_stats",
    }
)
WRITES = frozenset({"create_routine", "update_routine", "delete_routine", "delete_workout"})
ACCOUNT = SAFE - {"search_exercise_templates"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--public-only", action="store_true")
    result.add_argument("--workout-id")
    result.add_argument("--routine-id")
    result.add_argument("--exercise-template-id")
    result.add_argument(
        "--pace", type=float, default=2.0, help="Seconds between MCP reads, minimum 2"
    )
    return result


async def run(client: Any, args: argparse.Namespace, *, sleep: Any = asyncio.sleep) -> int:
    if not 2 <= args.pace <= 60:
        raise ValueError("pace must be 2..60 seconds")
    inventory = await client.list_tools()
    tools = {tool.name: tool for tool in inventory}
    if len(tools) != len(inventory) or set(tools) != SAFE | WRITES:
        print("FAIL inventory: missing or unreviewed tools")
        return 1
    for name in SAFE:
        tool = tools[name]
        hints = tool.annotations
        if (
            not hints
            or hints.read_only_hint is not True
            or hints.destructive_hint is not False
            or hints.idempotent_hint is not True
            or not tool.output_schema
            or tool.output_schema.get("type") != "object"
        ):
            print("FAIL inventory: SAFE contract changed")
            return 1
    called: set[str] = set()
    failures = skips = degraded = 0
    account_available = True

    def skip(name: str, reason: str) -> None:
        nonlocal skips
        if name in called:
            raise RuntimeError("repeated SAFE check")
        called.add(name)
        skips += 1
        print(f"SKIP {name}: {reason}")

    async def call(name: str, **arguments: Any) -> Any:
        nonlocal failures, skips, degraded, account_available
        if name not in SAFE or name in called:
            raise RuntimeError("unreviewed or repeated invocation")
        if name in ACCOUNT and (args.public_only or not account_available):
            skip(name, "public-only" if args.public_only else "no usable preexisting session")
            return None
        called.add(name)
        try:
            try:
                result = await client.call_tool(name, arguments, raise_on_error=False)
                value = result.structured_content
                validate_output(name, value)
            finally:
                await sleep(args.pace)  # Completion-based pacing, including failed reads.
        except Exception:
            failures += 1
            print(f"FAIL {name}: protocol or schema failure (details suppressed)")
            return None
        if result.is_error:
            code = Failure.model_validate(value).error.code
            if name in ACCOUNT and code == "AUTHENTICATION_ERROR":
                account_available = False
                skips += 1
                print(f"SKIP {name}: no usable preexisting session")
            elif code in {"CATALOG_UNAVAILABLE", "CAPABILITY_UNAVAILABLE"}:
                degraded += 1
                print(f"DEGRADED {name}: {code}")
            else:
                failures += 1
                print(f"FAIL {name}: {code}")
            return None
        if "error" in value:
            failures += 1
            print(f"FAIL {name}: error flag missing")
            return None
        scope = value.get("scope", {})
        if (
            value.get("truncated")
            or value.get("warnings")
            or (name == "get_exercise_stats" and not scope.get("all_accessible_history_included"))
        ):
            degraded += 1
            reasons = []
            if value.get("truncated"):
                reasons.append("truncated")
            if value.get("warnings"):
                reasons.append("warning")
            if name == "get_exercise_stats" and not scope.get("all_accessible_history_included"):
                reasons.append("incomplete history scope")
            print(f"DEGRADED {name}: {', '.join(reasons)}")
        else:
            print(
                f"PASS {name}: count={value.get('returned_count', len(value.get('result', [])) if 'result' in value else 1)}"
            )
        return value

    await call("search_exercise_templates", limit=1)
    page = await call(
        "list_workouts", page_size=1, format="summary", include_meta=True, scan_limit=5
    )
    workout_id = args.workout_id or (page["items"][0]["id"] if page and page["items"] else None)
    detail = (
        await call("get_workout", workout_id=workout_id, include_raw=False) if workout_id else None
    )
    if not workout_id:
        skip("get_workout", "no workout fixture")
    await call("get_recent_workouts", limit=1, format="summary", include_meta=True, scan_limit=5)
    routines = await call("list_routines", format="summary", include_meta=True)
    routine_id = args.routine_id or (
        routines["items"][0]["id"] if routines and routines["items"] else None
    )
    if routine_id:
        await call("get_routine", routine_id=routine_id, include_raw=False)
    else:
        skip("get_routine", "no routine fixture")
    exercises = (detail or {}).get("exercises", [])
    template_id = args.exercise_template_id or (
        exercises[0]["exercise_template_id"] if exercises else None
    )
    if template_id:
        history = await call(
            "get_exercise_history",
            exercise_template_id=template_id,
            page_size=1,
            include_meta=True,
            scan_limit=5,
            include_raw=False,
        )
        if history and history.get("items"):
            await call(
                "get_exercise_stats", exercise_template_id=template_id, limit=1, scan_limit=5
            )
        else:
            skip("get_exercise_stats", "no real history fixture")
    else:
        skip("get_exercise_history", "no exercise fixture")
        skip("get_exercise_stats", "no exercise fixture")
    for uri in ("hevy://help", "hevy://templates"):
        try:
            try:
                resource = await client.read_resource(uri)
            finally:
                await sleep(args.pace)
            if len(resource) != 1 or resource[0].mime_type != "application/json":
                raise ValueError("invalid resource content")
            value = json.loads(resource[0].text)
            if "error" in value:
                Failure.model_validate(value)
                degraded += 1
                print(f"DEGRADED {uri}: typed error")
            elif uri == "hevy://help":
                Help.model_validate(value)
                print(f"PASS {uri}")
            else:
                catalog = TemplateSearchResult.model_validate(value)
                if catalog.warnings:
                    degraded += 1
                    print(f"DEGRADED {uri}: catalog warning")
                else:
                    print(f"PASS {uri}")
        except Exception:
            failures += 1
            print(f"FAIL {uri}: protocol or schema failure (details suppressed)")
    assert called == SAFE
    print(f"SAFE result: failures={failures} skips={skips} degraded={degraded}")
    return 1 if failures else 2 if skips or degraded else 0


async def main_async(args: argparse.Namespace) -> int:
    if not args.public_only:
        try:
            status = SessionManager().status()  # Offline; never refresh to pass preflight.
        except Exception:
            print("SKIP preflight: no usable preexisting session; no transport opened")
            return 2
        if status["expired"] or status["expires_at"] is None:
            print("SKIP preflight: expired or unknown session expiry; operator action required")
            return 2
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    transport = StdioTransport(
        command="/usr/bin/env",
        args=["-u", "PYTHONPATH", sys.executable, "-m", "hevy_mcp.cli", "serve"],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
        keep_alive=False,
    )
    try:
        async with Client(transport) as client:
            return await run(client, args)
    except Exception:
        print("FAIL runner: protocol or schema failure (details suppressed)")
        return 1


def main() -> int:
    args = parser().parse_args()
    if not 2 <= args.pace <= 60:
        print("FAIL pace: expected 2..60 seconds")
        return 1
    print("SAFE reads only. Session refresh may rotate credentials. No login or writes.")
    try:
        approved = input("Type SAFE to start the live read checks: ") == "SAFE"
    except (EOFError, KeyboardInterrupt):
        approved = False
    if not approved:
        print("SKIP: confirmation declined")
        return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
