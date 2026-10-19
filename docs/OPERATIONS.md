# Operations

Runbook for running this server against a real account. It covers setup, credentials,
read recipes, catalog maintenance, the delete-transport check, the read-only smoke
runner and packaging. Authoritative behaviour lives in [DESIGN.md](DESIGN.md).

## Guarded setup

Use Python 3.12 or 3.13. A stray `PYTHONPATH` in the shell can make imports resolve to
another checkout, so remove it before the interpreter starts, during setup and for
every subprocess launch. Clearing it inside Python after imports is too late.

```bash
cd /path/to/hevy-mcp
umask 077
uv sync --frozen --all-groups
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m pytest
.venv/bin/python scripts/verify_wheel.py
```

Prefix those commands with `/usr/bin/env -u PYTHONPATH` when the surrounding shell
exports the variable.

`verify_wheel.py` builds the wheel and sdist offline, compares exact bytes and names
against the working tree, and installs them into isolated 3.12 and 3.13 environments
from the local uv cache. It needs both interpreters and a populated cache: a missing
cache is a setup failure, never a reason to download during a test run. It does not
need Chromium. `scripts/smoke_test.py` is excluded from pytest collection and is never
part of a default test run.

Run offline checks with the network denied. Child processes are sandboxed before their
imports, so no test touches a real account, a browser or a live endpoint.

## Credentials and browser login

The config directory defaults to `~/.config/hevy-mcp` (or `$XDG_CONFIG_HOME/hevy-mcp`)
and can be overridden with `HEVY_CONFIG_DIR`. Keep the directory at mode 0700 and the
credentials, settings and snapshots at 0600. Credentials are written atomically and
refreshes are coalesced across processes with a file lock, so never copy or overwrite an
active credentials file by hand, and never put a token, cookie or password into a tool
argument, a log or a support request.

Browser installation and login are explicit operator actions:

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/hevy-mcp auth login            # add --email you@example.com to check the account
.venv/bin/hevy-mcp auth status
.venv/bin/hevy-mcp doctor
```

`auth login` opens the Hevy page in a real browser and captures the normal session
cookie; this server stores the session itself and disables the dependency's own
persistence. Missing Chromium fails safely and never triggers an installation. The MCP
server never performs an interactive login and never opens a browser. `auth logout`
removes the local session and leaves the remote one untouched.

`doctor` inspects local state and browser availability without calling Hevy, and exits
nonzero when there is no session. `doctor --live` may refresh and persist tokens, so
run it deliberately. Reads can rotate credentials, which means a read-only check is not
free of local side effects.

Check expiry offline before any account-wide smoke:

```bash
.venv/bin/hevy-mcp auth status
```

If the session is missing, expired or of unknown expiry, stop. `refreshable=true` is not
permission to refresh: renew the session with `auth login`, then re-run status. This CLI
has login, status and logout, and no separate refresh command. One account-active
process at a time, even though the refresh lock is shared.

## Read recipes and limits

Start with `list_workouts(page_size=5, format="summary", include_meta=true)`, then read a
chosen ID with `get_workout(include_raw=false)`. Prefer
`search_exercise_templates(query="bench", equipment_category="barbell")` over guessing
IDs; that tool needs no credentials and does not enter the account lane.

Date windows are inclusive on session start, normalized to UTC and applied locally.
Each scanner call consumes at most 200 source rows, 40 upstream batches and 120 seconds,
and requests stay inside the remaining budget. Resume with `next_cursor` using the same
window, size and template, and `page=1`; do not follow cursors recursively. Offsets move
under you on a changing dataset, `has_more` is conservative and `page_count` is not
authoritative, so no read establishes a complete historical database. Exhausting a
source describes that endpoint's accessible pages only.

`get_exercise_stats` uses the same reducer as `include_stats=true`. Each continuation is
its own aggregate, and `total_seen` is scan provenance rather than a running total.
Deduplicate by workout and template IDs before combining batches, and never sum best
e1RM values or duplicate volume. All-time records stay false without independent evidence
that upstream returned everything.

API facts worth keeping in mind, with the binding contracts in
[DESIGN.md](DESIGN.md#43-exact-routine-mutation-input-and-translation):

- The source cap is five rows per batch, not five pages.
- A singular routine GET can arrive wrapped as `{"routine": {...}}`. Routine sync needs
  the `updated`, `deleted` and `isMore` fields and rejects an incomplete `isMore=true`.
- Routine sets describe prescriptions. Input `type` becomes the wire `indicator`, with a
  zero-based `index` and non-null measurements. Observed workout sets may carry extra
  fields, and those belong in strict input only when they are supported.
- RPE input accepts 0 to 10 including fractions. The routine translation does not
  transmit set RPE, set IDs or notes, exercise IDs or titles. Response RPE survives
  normalization when upstream sends it.
- Supersets use exactly `superset_id`, an optional integer, and never a camelCase alias.

Always read a routine before replacing it, preserve the fields you want to keep, and
inspect acknowledged IDs after an ambiguous write. There is no automatic replay and no
restore API.

## Catalog maintenance (offline only)

The bundled 451-row snapshot comes from a catalog store export. Its metadata records the
app bundle URL, the original capture time, the import time and both checksums. The
original bundle is not redistributed, so its checksum is recorded as unknown. The
snapshot is already older than the seven-day staleness threshold, which makes `stale`
informational rather than a refresh trigger.

Never call the dependency's catalog refresh at startup, in a search, at build time or in
tests: a refresh call can download even when it looks disabled, if its cache is missing
or stale. Import a source you already have explicitly:

```bash
.venv/bin/python scripts/import_catalog.py /path/to/_app-HASH.js \
  --kind local_js --source-url https://hevy.com/_next/static/chunks/pages/_app-HASH.js \
  --captured-at CAPTURE_TIME_IN_UTC --output-dir /path/to/review-output
```

Use the real hash and date. `--kind har` parses a HAR capture, and `--kind store_export`
loads a dependency store while checking its origin, time and count. Every mode validates
against the strict schema and checksum before writing. Keep raw JS and HAR captures
outside the repository and outside build artifacts.

For a local override, maintain both `exercise_catalog.json` and
`exercise_catalog.meta.json` in the config directory with umask 077 and atomic
replacement, while no process is reading them. An invalid override falls back to the
bundled data with a fixed warning, and with no usable source the tools return typed
`CATALOG_UNAVAILABLE`. A checksum detects corruption, not authorship: review provenance
before installing an override.

Custom exercise templates return `CAPABILITY_UNAVAILABLE` before any I/O, because the
custom envelope is not independently evidenced. Folder tools stay unregistered until
their request and response shapes have fixtures and a disposable live create and list
check has been reviewed. There is no evidenced workout create or update endpoint.

## Delete-transport check

`internal`, `v1` and `v2` are candidate names for the workout delete transport. They are
not three verified routes, and workout deletion stays disabled until one of them has been
validated against a disposable workout.

1. Create a workout you are willing to lose, titled exactly
   `Disposable Delete Test <unique nonce>`.
2. Read it with `get_workout` and note the canonical ID, title and UTC start time.
3. Pick one candidate and run the check deliberately:

```bash
.venv/bin/hevy-mcp test-delete-backend \
  --backend internal --workout-id DISPOSABLE_ID \
  --title 'Disposable Delete Test UNIQUE_NONCE' --started-at EXACT_UTC_TIME
```

The CLI prints the identity it is about to use and asks for confirmation. Declining
attempts nothing and persists nothing. There is no bypass or dry-run flag, and offline
tests cover the decline and unverified paths with fake clients. Only an exact identity
match plus a durable normalized and raw snapshot allows the single `DELETE`, and the
backend is written to `config.yaml` only after absence read-back confirms success. If the
outcome is unknown, still present or inconclusive, inspect the target and the snapshot
first. Never replay the request and never switch endpoints. Record shapes and outcomes,
never raw workout snapshots or tokens. Routine deletion has no equivalent guard.

## Read-only smoke runner

```bash
.venv/bin/python scripts/smoke_test.py --public-only     # no account access at all
.venv/bin/python scripts/smoke_test.py                   # needs a valid session
.venv/bin/hevy-mcp doctor --live                         # one harmless read
```

The runner requires typing `SAFE` before it opens stdio. It inventories the exact
reviewed 12 tools and refuses unknown or missing tools and any changed read-only hint,
annotation or schema. Then it invokes each eligible read once, serially, from a fixed
allowlist: `list_workouts`, `get_workout`, `get_recent_workouts`, `get_exercise_history`,
`list_routines`, `get_routine`, `search_exercise_templates`, `get_exercise_stats`. Writes
are excluded. There is no browser login, no mutation, no backend discovery and no replay.

Limits are one item per workout, history, catalog and stats read, `scan_limit=5`, one
full routine sync (upstream offers no evidenced page control) and two local resources
(`hevy://help`, `hevy://templates`). Workout and routine IDs come from preceding
successful list reads; history uses an exercise from the selected workout, and stats runs
only after a nonempty history response. Real IDs can be supplied instead. Catalog IDs are
never substituted for account history. A missing fixture or session, or `--public-only`,
produces an explicit skip. Pacing between checks is two seconds at minimum.

Exit 0 means every check passed with nothing skipped or degraded, 1 means a failure and
2 means something was skipped or degraded. A partial run is never reported as full
acceptance, and bounded statistics that cannot cover accessible history count as
degraded. Resource clients must inspect the JSON error document, since MCP resources have
no `isError` flag.

## Packaging and installation

Build and install a reviewed artifact into its own environment:

```bash
uv build
uv venv --python 3.12 /path/to/venvs/hevy-mcp
uv pip install --python /path/to/venvs/hevy-mcp/bin/python dist/hevy_mcp-0.1.0-py3-none-any.whl
/path/to/venvs/hevy-mcp/bin/hevy-mcp --help
```

Both distributions contain the README and docs, the catalog JSON with its provenance,
and the attribution notice. In the wheel, the README and docs sit under `hevy_mcp_docs/`
next to `hevy_mcp/`. No references, `.env` files, credentials, config snapshots, workout
archives, captures, logs, virtual environments or caches belong in either artifact, and
the real config directory is never packaged. The catalog holds public exercise
definitions, not private workout data.

Register the installed server in your MCP client by pointing it at the console script
with an absolute path. Clients that support an `env` block can set `HEVY_CONFIG_DIR`
there. Keep one account-active process running at a time.

## Rollback

Stopping the client entry and starting the previous command is the whole rollback. It
does not undo writes that already reached Hevy, and it is not a restore mechanism:
never put stale credentials over rotated tokens, and never treat local snapshots as an
implemented restore API. Snapshot files exist as audit evidence after a deletion.
