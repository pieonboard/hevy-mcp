# hevy-mcp

**An unofficial [Hevy](https://hevy.com/) MCP server that runs on a free account.** Sign in once in a browser, then let an agent read your workouts, work with routines, search the exercise catalog offline, and compute strength numbers from your own history.

![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-stdio-6f42c1)
![Built with FastMCP](https://img.shields.io/badge/built%20with-FastMCP-4B8BBE)
![License](https://img.shields.io/badge/license-MIT-green)
![CI](https://github.com/pieonboard/hevy-mcp/actions/workflows/ci.yml/badge.svg)
![Status](https://img.shields.io/badge/status-pre--release-orange)

> Pre-release. Reads are implemented, covered by the offline suite and exercised against the live API. Creating, replacing and deleting routines was verified end to end on a disposable routine. Workout deletion stays off until you validate a delete transport against a workout you are willing to lose. Hevy's private endpoints can change without notice.

## Contents

- [Why this exists](#why-this-exists)
- [Features](#features)
- [Tools](#tools)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Safety and honest limits](#safety-and-honest-limits)
- [Upstream API notes](#upstream-api-notes)
- [Verification](#verification)
- [Documentation](#documentation)
- [Credits](#credits)
- [License](#license)

## Why this exists

Hevy's public API is available to Pro accounts only, so a server built on it needs an API key from a paid subscription. This project uses the private endpoints the Hevy apps themselves use, with the session created by your own browser login. Everything runs locally over stdio and calls Hevy directly, so there is no hosted component and no third party holding your data.

Sessions come from `hevy-mcp auth login`, which opens a browser once and writes the result, mode `0600`, into your config directory. Refreshes happen locally under a process lock, and the MCP server itself never asks for a password and never opens a browser. Estimated 1RM (Epley), heaviest set, max-reps set, external-load volume and rep zones are computed from the history each call reads, with the source workout and set IDs kept so a number can be traced back to the set behind it. Exercise search works offline and costs nothing, because a snapshot of Hevy's 451 built-in templates ships with the package along with its provenance, checksum and a staleness flag.

The trade is real too. Private endpoints are undocumented, so schemas can drift; history access is bounded by the paging described in [Upstream API notes](#upstream-api-notes); and there is no create or update workout endpoint to call, so this server will never log a workout for you. It reads, analyses, manages routines, and deletes only objects you identify exactly.

## Features

- Workout reads in two sizes: compact summaries with IDs, and complete sessions with every set, plus recent-session windows and per-exercise historical sets for load and progression decisions.
- Deterministic exercise search over the bundled catalog, with exact `muscle_group` and `equipment_category` filters and a local override path for your own snapshot.
- Routine creation and full replacement from structured exercises and sets, sent once and followed by a read-back of the acknowledged object.
- Routine deletion with verified absence. Workout deletion needs an exact ID, title and start time, and writes a private snapshot before the single `DELETE`.
- Output modes that cut tokens: `format="summary"` when you want the shape only, `include_raw=false` when you do not need the upstream payload.
- Bounded scans. Each call stops inside a fixed budget and hands back a continuation cursor, so a resumed scan reads the same data in the same order.
- Read-only `hevy://` resources for help, workouts, routines and catalog entries, which a client can load without spending a tool call.
- Typed failures with an actionable hint on every code, one serialized lane for all account traffic, and structured logs on stderr with secrets redacted.

## Tools

The release profile publishes 12 tools. Reads carry `readOnlyHint=true`. Writes carry `destructiveHint` or `idempotentHint` where they apply, so a client can gate them under its own approval policy.

| Tool | What it does |
|---|---|
| `list_workouts` | Compact workout summaries and IDs. `page_size` 1 to 50, optional `since`/`until` window with continuation |
| `get_workout` | One complete workout: exercises, sets, notes, and the canonical ID, title and start time used for deletion |
| `get_recent_workouts` | Recent complete sessions for training context (10 by default, 100 maximum) |
| `get_exercise_history` | Normalized historical sets for one exercise template (20 by default, 100 maximum), optionally with local stats attached |
| `list_routines` | Every routine, normalized, with a summary mode for resolving IDs |
| `get_routine` | One routine in full. Read it before replacing it |
| `get_exercise_stats` | Estimated 1RM, heaviest set, max-reps set, volume and rep zones for one exercise (100 observations by default, 1000 maximum) |
| `search_exercise_templates` | Offline catalog search by title tokens, muscle group and equipment (20 by default, 100 maximum) |
| `create_routine` | Create a routine from structured exercises and sets, then read the acknowledged ID |
| `update_routine` | Replace a routine in full, exercise and set arrays included. Read first, then send the complete desired state |
| `delete_routine` | Permanent delete after an explicit request, with verified absence |
| `delete_workout` | Permanent delete of one identified workout, needing an exact ID, title and start time, a configured delete transport and a private local snapshot |

Statistics describe the history the call actually read, so treat them as scoped rather than all time.

## Quick start

### 1. Install

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/pieonboard/hevy-mcp.git
cd hevy-mcp
uv venv --python 3.12
uv sync --frozen --all-groups
```

### 2. Sign in (one time)

The login flow opens a real browser window, and the browser download below is only needed for it:

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/hevy-mcp auth login
.venv/bin/hevy-mcp auth status
```

### 3. Connect an MCP client

Any client that speaks stdio MCP works, on macOS, Linux or Windows. Point it at the console script with an absolute path. Stdout carries protocol traffic only, logs go to stderr.

Claude Desktop, in `claude_desktop_config.json` (`~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows):

```json
{
  "mcpServers": {
    "hevy": {
      "command": "/path/to/hevy-mcp/.venv/bin/hevy-mcp",
      "args": ["serve"]
    }
  }
}
```

Cursor (`~/.cursor/mcp.json`), Windsurf, Cline and most other clients use the same shape.

Claude Code:

```bash
claude mcp add hevy -- /path/to/hevy-mcp/.venv/bin/hevy-mcp serve
```

Codex CLI:

```bash
codex mcp add hevy -- /path/to/hevy-mcp/.venv/bin/hevy-mcp serve
```

A client that supports an `env` block can point the server at another config directory there, for example `"env": {"HEVY_CONFIG_DIR": "/path/to/config"}`.

### 4. Sanity check

```bash
.venv/bin/hevy-mcp doctor          # config, session file and browser availability, offline
.venv/bin/hevy-mcp doctor --live   # the same, plus one harmless read
```

An expired session that can still be refreshed is normal. The next call refreshes it, or run `doctor --live` once. `auth logout` deletes the local session and leaves the remote one alone.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `HEVY_CONFIG_DIR` | `~/.config/hevy-mcp` (or `$XDG_CONFIG_HOME/hevy-mcp`) | Session, refresh lock, `config.yaml` and snapshots |
| `hevy.workout_delete_backend` in `config.yaml` | unset | The single delete transport used by `delete_workout` (`internal`, `v1` or `v2`). Unset means deletion is off |

There is no `.env` loader and nothing is read from the working directory. A catalog JSON and metadata pair placed in the config directory overrides the bundled catalog. An invalid override falls back to the bundled data and reports `invalid_local_catalog_ignored`, and when no source is usable the tools return `CATALOG_UNAVAILABLE`.

Deletion is off by default on purpose. Pick a workout you created only for this test, validate one candidate transport with `hevy-mcp test-delete-backend`, and that choice is written to `config.yaml` only after the delete has been confirmed by absence. Never probe all candidates, replay a write, or let a failed endpoint fall through to another one.

## Architecture

```
src/hevy_mcp/
├── cli.py          Typer CLI: auth, serve, doctor, delete-transport validation
├── server.py       FastMCP factory, error-boundary middleware, stdio entry point
├── runtime.py      single account lane, cancellation drain, mutation observation
├── service.py      domain operations over the unofficial client
├── raw_client.py   transport over the pinned unofficial client
├── reads.py        finite history scanner, projections, date windows, cursors
├── analytics.py    deterministic statistics (Epley 1RM, volume, rep zones)
├── catalog.py      catalog validation, checksum, local override, search
├── deletion.py     snapshot store, delete transports, absence verification
├── boundary.py     exception to typed error code mapping for tools and resources
├── results.py      result envelope, error view, strict output views
├── schemas.py      per-tool output schemas
├── resources.py    read-only hevy:// resources and server instructions
├── auth.py         session persistence, locked refresh, browser login
├── logging.py      redacting JSON logging on stderr
├── models.py       normalized DTOs and the strict routine input schema
└── config.py       config directory and settings store
```

A few decisions explain most of the behaviour:

- Every account call passes through one queue in `runtime.py`. Concurrent tool calls are admitted, drained and cancelled without interleaving requests to Hevy.
- `get_exercise_stats` and `include_stats=true` run the same reducer over the same normalized observations, so the two never disagree.
- A read stops at 200 source rows, 40 upstream batches or 120 seconds, whichever comes first. It returns a `next_cursor`, and refuses a cursor replayed with a different window or size.
- A write that may have been delivered returns `MUTATION_INDETERMINATE` with the target and snapshot context, rather than retrying or trying another endpoint.
- Workout titles, notes and raw payloads are treated as data. They cannot authorize an action, reveal a credential or change policy, and the server instructions say so.

## Safety and honest limits

| Limit | Behaviour |
|---|---|
| Private endpoints drift | Typed `UPSTREAM_SCHEMA` or `ENDPOINT_CHANGED` failures instead of guesses. Offline fixtures prove local behaviour, not what the remote currently accepts |
| Session refresh | A refresh rotates the stored credentials. Run one account-active process at a time |
| Historical access | No completeness promise. Window metadata, `has_more` and cursors report what was scanned |
| Writes | One wire attempt, no replay and no fallback. Inspect the known target and snapshot before any deliberate recovery |
| Destructive operations | Need an explicit request. A workout snapshot is audit evidence, not a restore API |
| Missing capabilities | Custom exercise templates, folders, workout logging and measurements return `CAPABILITY_UNAVAILABLE` before any I/O instead of guessing an endpoint |

## Upstream API notes

These are behaviours of the private client this server wraps. They are handled internally, and they explain what the tools return.

- The history endpoint answers with fixed five-row batches and ignores a `limit` parameter. The scanner slices an oversized batch down to the requested size and advances its cursor by the rows it consumed. List reads that do honour `limit` still fail closed when a source returns more than asked for.
- `GET /routines/{id}` answers either with a bare object or wrapped as `{"routine": {...}}`. Both are parsed, and the raw view keeps the original envelope.
- Routine input is prescription data: `type`, `weight_kg`, `reps`, `distance_meters`, `duration_seconds`, `rpe`, `notes` and `id`. Wire sets use a zero-based `index`, an `indicator` and non-null measurements. Workout history is observed data and may carry extra or missing measurements.
- Supersets use exactly `superset_id`, an optional integer on an exercise. Not `supersetId`, and not a copied response field.
- RPE accepts a decimal from 0 to 10. The current routine translation does not transmit RPE, set IDs or notes, or exercise IDs. Response RPE is preserved when the API sends it.

Always read a routine, project it into the strict input schema, and keep every exercise and set you want to remain. `update_routine` replaces the whole object.

## Verification

```bash
# Offline: sockets denied, synthetic fixtures, no credentials
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m pytest
.venv/bin/python scripts/verify_wheel.py
```

564 offline tests cover protocol behaviour, normalization, cursor arithmetic, statistics, error mapping, snapshot integrity, resources and the installed wheel on Python 3.12 and 3.13. The suite denies sockets, so it touches no credentials, no browser and no network. Lint, format and strict `mypy` are clean.

`scripts/smoke_test.py` is a separate read-only runner for the live API. It calls one reviewed tool at a time from a fixed allowlist and refuses unknown or re-annotated tools. It never runs from the test suite or at startup, and it needs a refreshable session and one active account process.

To audit dependencies against the pinned versions:

```bash
uv export --frozen --no-dev --no-hashes --format requirements-txt > requirements-audit.txt
uvx pip-audit -r requirements-audit.txt
```

Both commands need network access and belong to maintenance, not to the test run.

## Documentation

| Document | Contents |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | Implementation blueprint: architecture, tool contracts, scanning and projection rules, mutation translation, test plan |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Setup, verification recipes, wheel installation, smoke and deletion checklists |
| [`tests/README.md`](tests/README.md) | What the offline suite covers, module by module |

## Credits

- [Sahil624/hevy-unofficial](https://github.com/Sahil624/hevy-unofficial) performs authentication and transport for this server.
- [FastMCP](https://github.com/jlowin/fastmcp) and the [Model Context Protocol](https://modelcontextprotocol.io) provide the server framework and the protocol.
- Exercise definitions and media references come from Hevy and are redistributed as an offline snapshot, with provenance recorded in [`ATTRIBUTION.md`](src/hevy_mcp/data/ATTRIBUTION.md).

Not affiliated with, endorsed by or supported by Hevy. Use your own account, at personal volume, within Hevy's terms of service.

## License

MIT. See [LICENSE](LICENSE). Third-party notices for the unofficial client, FastMCP and the redistributed exercise catalog stay in effect as described under [Credits](#credits).
