# Hevy MCP v2 implementation design

## 1. Goals, scope and evidence

Port the working normal-session Hevy server to **FastMCP 4.1.0**, preserving its ten tools, CLI, credential layout, routine DTOs and guarded deletion. The product remains **Hevy's free tier, without a Pro developer API key**. Add compact reads, honest pagination, local date filtering and analysis, better agent instructions, resources, catalog search and scoped statistics. Workout logging is a separate, gated extension.

This document is an implementation blueprint, not an implementation or a claim of present Hevy endpoint availability. It records the plan only: no server registration, login, credential inspection or Hevy call happens here. Framework claims below were checked against installed distributions in isolated environments under `/tmp`.

### 1.1 Source precedence

| Evidence | Authority / use |
|---|---|
| Predecessor server behaviour | The ten historical tool signatures, limits, annotations and error vocabulary this rebuild preserves. A conflict with them is a design defect. Tool contracts are restated in §4. |
| Installed FastMCP / MCP / hevy-unofficial | Local API and source evidence, recorded in §2. Does not establish remote responses. |
| Predecessor packaging and README | Dependency baseline, installation and browser contract, operational tone and known live limitations. |
| Sibling server in this repository family | Structural precedent only: factory, explicit schemas, middleware, safe hints, operation lane. Its account model, synchronous handlers, pacing constants and domain behaviour are not Hevy requirements. |

Terminology:

- **Verified-source**: found in the pinned local code, tests or installed package.
- **Probed-framework**: actually exercised locally, without Hevy, during this design.
- **Asserted**: proposed public contract or upstream shape awaiting evidence; never a claim of live support.
- **Unavailable**: absent from the pinned client's surface.
- **Must-verify-live**: requires an explicitly invoked later smoke using disposable or selected account data. Mock success cannot close it.

The predecessor passed **151/151** cases again in a temporary editable copy under Python 3.12.13 with `pytest --disable-socket --allow-unix-socket -q` (3.00 seconds). That verifies the reference, not a port which has yet to be written. Unix socket allowance supports asyncio's local wakeup socket; it does not permit TCP connections. Subprocess network isolation needs the additional controls in §10.

### 1.2 Binding scope decisions

| Item | Decision |
|---|---|
| A1 | Preserve the exact legacy payload when new options are omitted. Explicit compact modes omit raw data by default. Resolve the brief's conflicting raw defaults through the compatibility sentinel in §5.1. |
| A2 | Add continuation metadata, based on consumed source offsets rather than `page_count`. Keep legacy page fields and success roots. |
| A3 | Compute deterministic exercise statistics locally; expose through `include_stats` and the B5 tool. |
| A4 | Add MCP date arguments where absent; filter locally in a bounded scanner. Never send unsupported date filters to workout endpoints. |
| C1 / C2 | Ship agent-oriented descriptions/instructions and read-only `hevy://` resources/templates with the tool surface. |
| B4 / B5 | Planned as separate contracts in the release phase; no Pro endpoints. |
| B3 | October amendment promotes a minimal folder list/create pair to a conditional addition in the release phase. Include concrete contracts and gates; folder update/delete/reorder remain sketches. |
| B1 | Approved direction, **unavailable** in WorkoutsAPI 0.0.2a0. Dedicated evidence gate; disabled and unregistered until verified. |
| B2 / other B3 / export | Sketch only; no promised implementation, default tool, endpoint or new CLI verb. |

No scheduler, coaching/social features, hosted HTTP service, multi-account credential system, Pro adapter, automatic backend discovery/fallback, or redesign/vendoring of hevy-unofficial. Do not repoint a running server during implementation. Registering the rebuilt server follows offline acceptance, and the switch itself is a separate operational action.

## 2. Verified framework and dependency surface

### 2.1 Reproducible inspection record

Inspection date: **2026-10-17**. Two scratch environments were used:

- `/tmp/hevy-design-venv`: CPython **3.12.13**.
- `/tmp/hevy-design-py313-root-20261017`: CPython **3.13.5**.

Both contained `fastmcp==4.1.0`, `fastmcp-slim==4.1.0`, `mcp==2.0.0`, `mcp-types==2.0.0`, `hevy-unofficial[browser]==0.0.2a0`, `httpx==0.28.1`, `pydantic==2.13.5`, `typer==0.17.4`, `click==8.3.3`, `pyyaml==6.0.2`; the browser extra resolved Playwright **1.64.0**. The second environment initially resolved newer Pydantic/PyYAML; these were aligned before comparison. These are observed installed versions, not an instruction to hand-author a lockfile.

All manual Python/uv invocations used `/usr/bin/env -u PYTHONPATH`. Reproduce with `uv venv --python 3.12 /tmp/<unique-name>` and `uv pip install --python /tmp/<unique-name>/bin/python` plus the pins above. Probe with `inspect.signature`, `inspect.getsource`, `importlib.metadata.version/requires`, and synthetic clients; no authentication is needed. Scratch probe files are not runtime dependencies or durable evidence links.

The [official middleware documentation](https://gofastmcp.com/servers/middleware) and [resource documentation](https://gofastmcp.com/servers/resources) were consulted for orientation. Exact 4.1.0 wheel behavior, not moving web documentation, controls this design.

### 2.2 Framework facts and their consequences

| Surface | Observed API / result | Implementation consequence |
|---|---|---|
| Import / replacement | `from fastmcp import FastMCP, Context` works. No `FastMcp` export. SDK `from mcp.server import MCPServer` still exists in MCP 2.0.0. | Replace the factory/server class; this is not evidence that MCPServer was removed. Keep SDK protocol types and stdio client utilities. |
| Constructor | `FastMCP(name, instructions, *, version, lifespan, middleware, mask_error_details, strict_input_validation, …)`. | Use `instructions`, not a blind copy of SDK `description=`. One runtime per factory; no import-time account IO. |
| Tools | `tool(..., output_schema=…, annotations=…, run_in_thread=True, …)`; `add_tool(tool_or_callable)`; `add_middleware(middleware)`. | Explicit signatures and explicit output schemas. Async Hevy handlers remain async; the thread default applies to synchronous functions only. |
| Calls | `await server.call_tool(name, arguments, run_middleware=True)` returns `fastmcp.tools.ToolResult`. | Test through this public API and the MCP client, not `_tool_manager._tools`. |
| Annotations | `mcp.types.ToolAnnotations` Python fields are `title, read_only_hint, destructive_hint, idempotent_hint, open_world_hint`; wire aliases are camelCase. | Construct with snake_case as the predecessor does; verify wire aliases with `model_dump(by_alias=True)`. |
| Middleware | `Middleware.on_call_tool(context: MiddlewareContext[CallToolRequestParams], call_next: CallNext[…, ToolResult]) -> ToolResult`; context has `message`, `fastmcp_context`, method/source/type. | Install a boundary first. Its tool hook returns a typed ToolResult, not arbitrary JSON. Resource errors need their own hook. |
| Domain exceptions | A plain domain exception becomes `ToolError` with the original in `__cause__` before reaching middleware. A subclass of `fastmcp.exceptions.ToolError` survives unchanged. | Use the typed bridge in §7. Do not assume a middleware `except HevyError` can see every service failure directly. |
| Argument validation | `fastmcp.exceptions.ValidationError` reaches middleware; `n="1"` was coerced to integer by default; unknown keyword and null integer failed. | Keep predecessor-compatible coercion; do not silently enable strict JSON typing. Distinguish invalid input from failed response validation. |
| ToolResult | `ToolResult(content=…, structured_content=…, meta=…, is_error=False)`. Its `to_mcp_result()` returns SDK CallToolResult when metadata or error flag is present. | Explicit JSON text plus structured content. Use `is_error=True`, not only an `error` key. |
| Array result | FastMCP's typed list produces one JSON array text block and structured `{"result": […]}`; generated schema uses an object wrapper and `x-fastmcp-wrap-result`. SDK 2.0.0 with the old exact `list[dict[str, Any]]` annotation instead emits one JSON text block per item, zero blocks for an empty list, with the same structured wrapper. | Explicit emission must preserve the SDK's per-item blocks for bare-list results; do not accept FastMCP's default conversion blindly. Never force an `items` wrapper on old calls. |
| Context | `Context(fastmcp, session=None, …)`; properties include `fastmcp`, `lifespan_context`, `request_id`, state/progress methods. A `ctx: Context` parameter is omitted from the input schema. | Context is framework DI, not a caller field. Prefer injected runtime closures; Context may carry safe progress/correlation data. |
| Lifespan | Async context-manager lifespan ran on MainThread with a running loop; yielded mapping appeared in `ctx.lifespan_context`; teardown ran on client exit. | Create loop-owned locks inside one lifecycle; close resources at shutdown, without login/probes. |
| Resources/templates | `@server.resource(uri, mime_type=…, …)`; URI placeholders automatically register templates. `read_resource(uri, run_middleware=True)` returns ResourceResult. | Static discovery URIs and bounded parameterized templates can call the same read facade. No separate account client. |
| Resource result/boundary | `fastmcp.resources.ResourceResult(contents, meta=None)`, `ResourceContent(content, mime_type=…)`; `on_read_resource(…)->ResourceResult`. | Emit JSON MIME explicitly, including typed failure documents; resource results have no ToolResult `is_error` flag. |
| Stdio | `run(transport="stdio", show_banner=False)`; `run_stdio_async(show_banner=True, log_level=None, stateless=False)`. | CLI owns the loop via `run`; async hosts can await the async entry, never nest `asyncio.run`. |

Synthetic in-process probes exercised context injection, lifespan, arrays, argument failures, the exception bridge, resource/template listing and JSON reads on both Python versions. Additional 3.12 probes compared SDK results with the predecessor's exact dict/list annotations, exercised explicit success/error object schemas, and confirmed a resource bridge reaches on_read_resource and becomes JSON MIME content. Real local stdio subprocesses initialized successfully, listed tools/templates, called a typed tool and read a template on both versions. No Hevy requests were involved. Middleware dispatch supplies the keyword `call_next`; preserve that parameter name in overrides rather than renaming it to an arbitrary positional alias.

### 2.3 Client surface: established methods versus unverified bodies

Inspected `HevyClient.__init__`, `request`, `HevyConfig`, `resources/{workouts,routines,exercises,misc}.py`, and `exercise_catalog.py` in the installed wheel.

| Area | Verified-source signature / implementation | Evidence limit |
|---|---|---|
| Session clients | `HevyClient(access_token=…, refresh_token=…, user_id=…, config=HevyConfig(auto_refresh=False), http_client=…)`; methods are synchronous. | Factory construction made no request. `request()` can replay a 401 when auto_refresh is enabled: explicitly disable it for normal clients. |
| Workouts | `get(id)`, `list_paged(*, username, limit=10, offset=0, extra_params=None)`, `get_batch(batch_id)`, `iter_paged`, `count`, `calendar`, `metrics`, images/comments/likes. | **No create, update, log, sync-write or delete method** on WorkoutsAPI. Existing workout deletion uses our separate raw client. |
| Batch reads | `get_batch(batch_id)` performs `GET /workouts_batch/{batch_id}`. | This is not proof of an events/delta-sync protocol, a batch-ID discovery scheme or continuation completeness. Do not implement incremental sync from this name. |
| Routines | Existing list/get/create/update/delete plus copy, sync_batch, update_locations. | Existing CRUD bodies are preserved from our service, not inferred from generic dict signatures. |
| Folders | `list_folders()`: GET /routine_folders; `create_folder(folder: dict, *, send_sync_event_to_mobile_app=True)`: POST /routine_folder with `{"folder": folder}` and sync query. Update sends the dict directly to PUT /routine_folder; delete takes str/int ID; reorder/get_shareable_folder exist. | Folder field names and response envelopes are **asserted**, because methods return Any and do not validate them. Minimal tool gates are §6.3. |
| Templates | `list_catalog(*, refresh=False, max_age: timedelta\|None=None)->list[dict]`; custom-template list/create/update/delete; list_template_units; history_paged(params); sets(template_id, page). | The parser's fields are inspectable; live custom-template/unit envelopes are not typed. No guessed “search” endpoint. |
| Export | `MiscAPI.email_download_link()`, `presigned_url(file_name)`. | No proof that these implement safe full-data download; email link creation has a user-visible side effect. Sketch only. |

**Catalog correction to the brief.** The 0.0.2a0 wheel contains the catalog parser/cache implementation, **no bundled catalog JSON**. `list_catalog()` calls `ExerciseCatalogStore.fetch_and_update()`: an empty/stale cache triggers GET hevy.com, discovery of the web app JS, then JS download/parse. `refresh=False` does not mean offline. The default freshness is seven days, and the default cache location is the library's own cache unless explicitly redirected. B4 must ship/read a separately evidenced snapshot and use `ExerciseCatalogStore.load()` or equivalent pure loading, never `list_catalog()` on the request path. No new browser scraper is needed.

### 2.4 Python / lifecycle pitfalls

The predecessor is **async service code around a synchronous client**, not a synchronous service needing an async shim. Keep async FastMCP handlers awaiting HevyService; retain `asyncio.to_thread(invoke)` and threaded close inside the adapter. Never put `asyncio.run(service.method())` in each synchronous FastMCP tool: it would create multiple loops around shared asyncio locks.

The worker probe found **no running or current policy loop** inside `asyncio.to_thread` on both 3.12.13 and 3.13.5. The inspected HevyClient uses synchronous httpx and does not require one. Do not install an artificial event loop in workers or assume FastMCP creates one there. If a later dependency starts calling `get_event_loop()`, that is a compatibility failure requiring a tested adapter decision.

Fresh main-thread `get_event_loop()` returned a selector loop with a DeprecationWarning on **both tested patch versions**; after `asyncio.run` it raised RuntimeError on both. Thus “works on 3.12, fails on all 3.13” is not an established fact. Implicit-loop behavior is version/context-sensitive; use `get_running_loop()` only inside async lifecycle, explicit runners only at CLI entry, and test 3.12 and 3.13. Do not raise the Python baseline to 3.13.

`SessionManager._refresh_locks` is class-level and keyed by canonical credential path. Preserve it, but do not share one live runtime across different event loops or repeatedly reuse contended locks after teardown. Tests use distinct config roots; factory/lifespan tests must also cover repeated creation on one loop and teardown/recreation. A cancelled await cannot prove a worker's wire request stopped; §8 retains the operation lane until work actually finishes.


## 3. Architecture and port boundaries

### 3.1 Package map and ownership

Distribution/repository name: `hevy-mcp`. Preserve import name `hevy_mcp` and console entry `hevy-mcp = hevy_mcp.cli:app`. Use hatchling and `src/` packaging.

| File / area | Responsibility |
|---|---|
| `src/hevy_mcp/server.py` | FastMCP factory, lifespan, middleware, instructions, quiet stdio. Keep injectable `create_server(service_factory=default_service)` for tests. |
| `tools/workouts.py`, `tools/routines.py` | Explicit ten-tool signatures, descriptions, annotations, output schemas; thin facade calls. Old files are placeholders, so this decomposition is new work. |
| `service.py`, `models.py`, `auth.py`, `config.py` | Faithful historical service, parsers, credential management and config. |
| `deletion.py`, `delete_client.py`, `raw_client.py` | Unchanged deletion state machine, exact candidate requests and raw read/write policy. |
| `errors.py`, `logging.py`, `cli.py` | Keep existing exceptions/redaction/CLI; add the boundary bridge, structured logging and safe CLI failure formatting as identified below. |
| `runtime.py` | Shared service/session references, async operation lane, cancellation drain, operation phase recorder, observed adapters/snapshot delegate. |
| `results.py` | Pydantic success projections, error/partial models, explicit schema composition and JSON text/structured emission. Do not overwrite upstream models to save tokens. |
| `reads.py` | Compatibility dispatch, bounded read scanner, checkpoint codec, A1/A4 projections. |
| `upgrade_client.py` | Only new low-level read/folder calls; same session/factory/error/retry discipline, no alternate auth store or library auto-refresh. |
| `analytics.py` | Pure A3/B5 statistics over normalized entries and scope/provenance. |
| `catalog.py`, `data/exercise_catalog.json`, `data/exercise_catalog.meta.json` | B4 validated local catalog, deterministic filtering, provenance/checksum. Data is a future acquired snapshot, not presently in the dependency. |
| `resources.py` | C2 bindings to the same facade and typed resource boundary. |
| `tools/discovery.py`, `tools/analytics.py`, `tools/folders.py` | B4/B5 and conditional minimal folders. |
| `workout_logging.py`, `tools/workout_logging.py` | Only after evidence recovery; absent from default registration before the gate. |
| `tests/`, `scripts/`, `docs/` | Ported tests plus framework/upgrade cases, separate smoke runner, OPERATIONS and evidence records. |

Layering: MCP tool/resource → projection/upgrade facade → existing HevyService or explicitly new client action → shared session/adapter → upstream. Statistics consume normalized objects only. CLI auth bypasses MCP but uses the same credential code. No framework dependency in copied service/parsers/deletion.

One factory builds one shared HevyService, one SessionManager and one configured deletion orchestrator. Runtime references are constructed without account IO. Async locks belong to the serving loop; lifespan establishes their ownership and drains in-flight operations on shutdown. Factory-injected fake services remain sufficient for all offline MCP tests. Do not cache live httpx clients globally: the predecessor creates and closes short-lived clients per adapter attempt.

### 3.2 What is copied verbatim

Paths below are under the predecessor source snapshot kept outside this repository; keep their corresponding `src/hevy_mcp/` names. “Verbatim” refers to existing function/model bodies during the scaffold and port phases, not a promise that a file may never gain imports or new declarations.

| Source location | Preserve literally / why |
|---|---|
| `service.py:44`, `_private_routine_payload`, `private_create_routine_payload`, `private_update_routine_payload` | Private DTO translation, zero-based set indexes, create UUIDs/index -1, update index and omitted unsupported fields. |
| `service.py:94`, `map_upstream_error` | Established HTTP/client error classification, including 5xx versus generic endpoint failure. |
| `service.py:126–285`, `UnofficialAdapter`, `PersistentUnofficialAdapter` | Five-row batching, authenticated username discovery, worker invocation, read retry/refresh limits, mutation ambiguity, client cleanup. |
| `service.py:287`, `HevyService`, all existing methods | Bounds, validation, schema rejection, workout/history/routine normalization, sync completeness and routine read-back. Upgrade facade delegates unchanged when options are omitted. |
| `models.py:13`, `normalize_timestamp`; model declarations and `parse_workout/parse_routine` | UTC second precision, aliases, permissive response extras, strict mutation input, complete-detail checks. Raw data remains available internally and in snapshots regardless of output projection. |
| `auth.py:22–159`, Session / UnofficialRefresher / SessionManager | Token layout, browser `save=False`, atomic persistence, expected-token comparison, process/local refresh coalescing, logout/status. |
| `config.py:13–74`, path helpers, atomic_write, SettingsStore | HEVY_CONFIG_DIR, 0700/0600, fsync, exclusive snapshot publication, YAML nesting/backend validation. |
| `deletion.py:30` onward, backend classes / SnapshotStore / DeletionOrchestrator | Exact ID guards, identity comparison, snapshot-before-attempt, reconciliation, no fallback. Wrap for phase recording; do not rewrite this state machine. |
| `delete_client.py:16`, SessionDeleteClient | Singular session GET, one read-401 refresh, exact candidate paths and internal mobile headers. |
| `raw_client.py:25`, RawHevyClient | 20-second timeout, read-only retry bound, one mutation attempt, 204 handling and malformed JSON behavior. |
| `errors.py`, all 15 existing exception classes | Names and inheritance remain available to service callers/tests. |
| `logging.py:17–99`, redaction constants/functions/filter | Complete sensitive header values, recursive structured values, token-key variants, URL query redaction. |

Line spans are navigation aids; named symbols control if snapshots move. No sorting/renaming/“cleanup” of copied logic during parity work. New scanners do not replace legacy batching, and original tests continue exercising the original paths.

### 3.3 What actually changes

- Replace `server.py:7` MCPServer import and `create_server/serve` registration (`server.py:63–130`) with FastMCP. Move the ten handlers to `tools/`; preserve required/positional argument order and annotation tuples.
- Replace untyped `dict[str, Any]` output declarations with explicit Pydantic-backed schemas and controlled ToolResult emission. Success payloads remain the old roots; failures gain stable JSON codes/hints.
- Keep `default_service` wiring (`server.py:50–60`), with runtime observation delegates and a typed preflight for an unconfigured delete backend. The old service's `RuntimeError` remains a direct-service behavior; the MCP facade exposes CONFIGURATION_ERROR before invoking it.
- Preserve `cli.py` commands/flags, JSON status/check output and backend tester guards. Repoint serve to FastMCP; format errors safely at CLI entry. Doctor's two sequential `asyncio.run` calls may be consolidated into one runner to avoid cross-loop lock reuse; still one harmless service read.
- Add A/C facade options/modules and B contracts; no retrofit of summaries into complete-workout parsers.
- Wrap the copied RedactionFilter with an allowlisted JSON formatter and route framework/library loggers to sanitized stderr. Prevent unredacted traceback formatting.
- Replace distribution metadata/dependency graph and docs. Add pytest-socket, build inspection and evidence records. No vendoring of hevy-unofficial.

The new operation lane serializes complete account operations, including read-before-write and verification. This is wrapper behavior; it does not change the copied adapters' wire attempts. It borrows the lane concept from Kleinanzeigen without importing its 1.5-second pacing, curl client or synchronous tool model.

## 4. Complete tool inventory and historical data contracts

### 4.1 Table conventions

Annotation tuple is **(readOnlyHint, destructiveHint, idempotentHint, openWorldHint)**:

- **R** = (true, false, true, false), matching all six historical reads.
- **W** = (false, false, false, false), matching routine create/update.
- **D** = (false, true, false, false), matching both deletes.
- **U** = (false, true, false, false) for the future full workout replacement: overwrite loses existing session data. This is a new-tool decision, not a change to update_routine's W annotation.

Open-world false preserves the predecessor's hints despite network access. Hints describe tool behavior; they do not grant permission, trigger automatic retries or replace caller policy.

All tool rows share **E**: §7's typed failure document and MCP error flag. Reads may retry bounded transient failures; mutations never replay. Empty lists are successful. Invalid response containers/details are SCHEMA_ERROR, not empty success.

Shared additive arguments, expanded fully in §5:

- **P** = `format: "summary"|"full"|None=None, include_raw: bool|None=None`.
- **Q** = `since: str|None=None, until: str|None=None, cursor: str|None=None, scan_limit: int[1..200]=200`.
- **M** = `include_meta: bool=False`. On arrays this explicitly selects an `{items, …metadata}` envelope; otherwise the legacy list/per-item text encoding remains.
- Q's dates/cursor activate bounded client scanning; scan_limit is meaningful on scanner paths only. All parameters beyond the legacy signature are optional, appended arguments. A default call uses the old path.

The signatures are public equivalences, not implementation code. `id` arguments remain nonempty strings validated by service; only guarded workout deletion imposes the historical safe-path character set.

| Tool | Type / stage | Signature equivalent | Success payload | Annotations | Error behavior |
|---|---|---|---|---|---|
| list_workouts | parity read, A1/A2/A4 | `(page: int>=1=1, page_size: int[1..50]=10, *, P, Q, M)` | WorkoutPage; optional full items and checkpoint metadata | R | E; malformed list/pagination fails closed; bounded scan reports its scope |
| get_workout | parity read, A1 | `(workout_id: str, *, P)` | Workout; summary projection only when requested | R | E; incomplete detail rejected; no summary substitution |
| get_recent_workouts | parity read, A1/A4 | `(limit: int[1..100]=10, *, P, Q, M)` | list[Workout]; explicit summary/envelope possible | R | E; never return malformed detail as a complete workout |
| get_exercise_history | parity read, A1/A3/A4 | `(exercise_template_id: str, page: int>=1=1, page_size: int[1..100]=20, *, P, Q, M, include_stats: bool=False)` | list[HistoryEntry]; requested per-entry stats, optional envelope aggregate | R | E; invalid sets/timestamps/IDs fail closed; no mutation |
| list_routines | parity read, A1 | `(*, P, M)` | list[Routine] | R | E; isMore=true / missing sync keys is SCHEMA_ERROR |
| get_routine | parity read, A1 | `(routine_id: str, *, P)` | Routine | R | E; accept direct or wrapped response; require exercises/sets |
| create_routine | parity write | `(routine: RoutineInput)` | Routine from singular read-back | W | E; ambiguous attempt/read-back failure has typed outcome; no replay |
| update_routine | parity full replacement | `(routine_id: str, routine: RoutineInput)` | Routine from singular read-back | W | E; preserve replacement DTO, never infer PATCH or replay |
| delete_routine | parity destructive | `(routine_id: str)` | `{deleted: true, routine_id: str}` only after 404 read-back | D | E; still present is ENDPOINT_CHANGED; unknown verification is typed partial |
| delete_workout | parity guarded destructive | `(workout_id: str, expected_title: str, expected_started_at: str)` | DeletionResult | D | E; zero attempts on identity/snapshot failure, one backend only, §8 state machine |
| search_exercise_templates | B4 read, release phase | `(query: str="", muscle_group: str\|None=None, equipment_category: str\|None=None, limit: int[1..100]=20, offset: int>=0=0, include_custom: bool=False, include_raw: bool=False)` | TemplateSearchResult, §6.1 | R | E; missing catalog explicit; no implicit remote catalog refresh |
| get_exercise_stats | B5 read, local computation | `(exercise_template_id: str, since: str\|None=None, until: str\|None=None, limit: int[1..1000]=100, cursor: str\|None=None, scan_limit: int[1..200]=200, include_raw: bool=False)` | ExerciseStatsResult, §6.2 | R | E; scoped records, never claim all-time PR from truncated history |
| list_routine_folders | B3 minimal, conditional | `()` | list[RoutineFolder], §6.3 | R | E; asserted response schema must be evidenced; no invented empty fallback |
| create_routine_folder | B3 minimal, conditional | `(title: str[min_length=1], index: int>=0=0)` | RoutineFolder after verified list/read-back | W | E; one POST, ambiguous result not recreated by title |
| create_workout | B1 gated | `(workout: WorkoutInput)` | verified Workout | W | E; proposed input, unavailable client transport; unregistered before gate |
| update_workout | B1 gated | `(workout_id: str, workout: WorkoutInput)` | verified Workout after full replacement | U | E; proposed input, unavailable client transport; no retries/automatic rollback |

The parity profile lists exactly **10 tools**. The release phase added B4/B5 for **12**, with the conditional verified minimal folder pair for **14**. The gated workout pair would bring it to **16**. Settings may provide `enable_discovery/enable_stats` (default true once their gate passes) and `enable_folders` (default false until evidence passes); `enable_workout_logging` is false and ineffective until its capability/evidence gate passes. A private compatibility test profile disables additions; it does not remove A/C optional functionality or any historical tool. No stub mutator is advertised as working.

Uncommitted sketches are included here to avoid mistaking available SDK methods for released tools:

| Possible tool / family | Proposed signature / result, not a registration commitment | Hint / unresolved contract |
|---|---|---|
| list_measurements | `(kind: str\|None=None, since: str\|None=None, until: str\|None=None, limit: int[1..100]=20, cursor: str\|None=None) -> MeasurementPage` | R; client method/route, units and paging unavailable/unverified |
| add_measurement | `(kind: str, value: float, unit: str, measured_at: str) -> Measurement` | W; asserted, one attempt; no guessed endpoint |
| update_routine_folder | `(folder_id: str\|int, title: str, index: int>=0) -> RoutineFolder` | W; SDK method exists, body/identity semantics asserted |
| delete_routine_folder | `(folder_id: str\|int) -> FolderDeletionResult` | D; whether contained routines move/delete is unverified, blocks enablement |
| reorder_routine_folders | `(folder_ids: list[str\|int]) -> list[RoutineFolder]` | W, idempotent hint conservatively false; upstream reorder item schema asserted |
| custom exercise template writes | create/update/delete models not yet fixed; B4 searches templates only | W/W/D; SDK dict CRUD exists, body/read-back contract not established |

These sketches use E if ever implemented. Do not write implementation stubs, enable flags or guessed HTTP bodies for the sketches in this design.

### 4.2 Exact normalized response models

Existing models serialize with `model_dump(mode="json")`, including null/default fields and response extras. Times are UTC ISO strings at second precision (normally Pydantic `Z` encoding); comparison accepts equivalent offsets. Reject boolean timestamps. Numeric timestamps mean Unix seconds, naive datetimes mean UTC, microseconds are removed. Preserve these rules for deletion identity too.

| Model | Complete fields and defaults |
|---|---|
| SetData | `id: str\|null=null; type: normal\|warmup\|dropset\|failure="normal"; weight_kg: float\|null=null; reps: int>=0\|null=null; distance_meters: float>=0\|null=null; duration_seconds: int>=0\|null=null; rpe: float[0..10]\|null=null; notes: str\|null=null`. Response extras allowed; type accepts alias indicator. Response weight has no nonnegative validator; do not “fix” this during port. |
| ExerciseData | `id: str\|null=null; exercise_template_id: str; title: str\|null=null; notes: str\|null=null; superset_id: int\|null=null; rest_seconds: int>=0\|null=null; sets: list[SetData]=[]`. Response extras allowed. |
| Workout | `id: str; title: str; started_at: datetime; ended_at: datetime\|null=null; notes: str\|null=null; exercises: list[ExerciseData]=[]; raw: dict={}`. |
| WorkoutSummary | `id: str; title: str; started_at: datetime; ended_at: datetime\|null=null; duration_seconds: int\|null=null; exercise_count: int`. No raw field in v1. |
| WorkoutPage | `items: list[WorkoutSummary]; page: int; page_count: int`. Compatibility fallback page_count is 1, **not a reliable total**. |
| HistoryEntry | `workout_id: str; exercise_template_id: str; started_at: datetime; sets: list[SetData]; raw: dict={}`. |
| Routine | `id: str; title: str; folder_id: int\|str\|null=null; notes: str\|null=null; exercises: list[ExerciseData]=[]; index: int=0; program_id: str\|null=null; coach_force_rpe_enabled: bool=false; raw: dict={}`. |
| DeletionResult | `workout_id: str; snapshot_path: str; backend: str; verified_absent: bool`. Successful orchestrator results have verified_absent=true. |
| RoutineDeleted | Typed replacement for the plain dict: `deleted: Literal[true]; routine_id: str`. Same JSON as v1. |

Workout parsing accepts id/workout_id, title/name, start_time/started_at, end_time/ended_at and description/notes, preserving the original raw object. ID/title must be non-null/nonblank; timestamp must exist. Summary parsing accepts absent exercises and uses explicit exercise_count, otherwise the list length, otherwise zero. Duration is calculated from end minus start; do not add clamping in parity mode.

A complete workout/routine must have an exercises list and each exercise must have a sets list, even when empty. Missing detail is not the same as an empty session. Routine parsing accepts `{"routine": {...}}` and IDs id/routine_id/routineId; raw retains the entire original envelope. Preserve unknown exercise/set response fields.

Exercise history accepts history/data lists or the live workout-row list: workout ID from workout_id/id, template from exercise_template_id or requested template, start from started_at/start_time; sets from top-level sets, else exercises[0].sets. The latter is the predecessor's verified reconciliation rule: do not silently substitute a different exercise during port. New fixtures should detect upstream drift, not “repair” malformed data.

Routine list requires `{updated: list, deleted: list, isMore: bool}` with every key. Return normalized updated rows only when isMore=false. Do not treat sync_batch/versions as proven continuation; isMore=true fails closed.

### 4.3 Exact routine mutation input and translation

RoutineInput and both nested input models forbid unknown fields.

- RoutineInput: required `title: str(min_length=1)`, required `exercises: list[RoutineExerciseInput]`; optional `folder_id: int|str|null=null`, `notes=null`, `index: int>=0=0`, `program_id: str|null=null`, `coach_force_rpe_enabled: bool=false`.
- RoutineExerciseInput: required nonblank `exercise_template_id: str`; `id/title/notes: str|null=null`, `superset_id: int|null=null`, `rest_seconds: int>=0|null=null`, `sets: list[RoutineSetInput]=[]`.
- RoutineSetInput: `id: str|null=null`, `type: normal|warmup|dropset|failure="normal"`, `weight_kg/distance_meters: float>=0|null=null`, `reps/duration_seconds: int>=0|null=null`, `rpe: float[0..10]|null=null`, `notes: str|null=null`.

Do not tighten title to trimmed/nonblank in parity: the current input only has min_length=1. Do not restrict RPE inputs to a speculative 6–10 ladder.

Wire routine body has title, folder_id, index, notes, program_id and exercises. Each exercise emits exercise_template_id, notes (null/empty becomes ""), rest_seconds (null/zero becomes 0), sets, and superset_id only when supplied. Sets emit zero-based index, indicator from type, and only non-null weight_kg/reps/distance_meters/duration_seconds. **Accepted input id/title fields on exercises, set id/rpe/notes and coach_force_rpe_enabled are not transmitted by the current translator.** This limitation is preserved and documented; no promise that every accepted field persists remotely.

Create uses index=-1 and matching newly generated UUID `clientId/_unsyncedObjectId`; update uses the supplied index and sends neither create ID. Create accepts a nonblank string routineId response or a parseable full routine response, then singular GET; update ignores the response body and singular GETs the target. Read-back IDs must match. It verifies identity, not exact persistence of every accepted input field.

Update is **full replacement**, including exercise/set arrays. Call get_routine first, project only RoutineInput-supported fields and preserve everything the DTO can express that should remain. Do not pass the response's raw/server fields straight into extra-forbid input; do not describe this as PATCH. Round-tripping unknown upstream fields is not a v1 guarantee.

Routine deletion has no workout-style title/time/snapshot guard: one delete, then get_routine. NotFound is success; still present is EndpointChangedError. Preserve that distinction.


## 5. Required A1–A4 and C1–C2 contracts

### 5.1 A1: projection and compatibility rules

`format=None` is a deliberate public compatibility sentinel. It is not an undocumented alias for summary. If P/Q/M/include_stats are omitted, the ten historical calls return the same JSON success values, null/default fields and roots as v1. Existing raw blobs remain available. A presentation upgrade must not modify stored models or deletion snapshots.

| Read | format=None | format="summary" | format="full" |
|---|---|---|---|
| list_workouts | WorkoutPage with WorkoutSummary items | Same normalized summary item fields | Page items are Workout objects; singular detail hydration per returned ID, at most page_size (50). This extra IO is explicit. |
| get_workout | Workout | WorkoutSummary projected from validated detail | Workout |
| get_recent_workouts | list[Workout] | list[WorkoutSummary] | list[Workout] |
| get_exercise_history | list[HistoryEntry] | list[HistorySummary] | list[HistoryEntry] |
| list_routines | list[Routine] | list[RoutineSummary] | list[Routine] |
| get_routine | Routine | RoutineSummary | Routine |

Additional projection models:

- RoutineSummary: `id, title, folder_id, index, program_id, coach_force_rpe_enabled, exercise_count`. Folder/program defaults remain null, index 0 and coach flag false. Notes/exercises are omitted.
- HistorySummary: `workout_id, exercise_template_id, started_at, set_count`; optional stats only when include_stats=true. Sets are omitted.
- WorkoutSummary is exactly the legacy summary model; get/recent summary exercise_count comes from validated complete exercises.

`include_raw` resolution is precise:

1. Explicit true includes raw; explicit false omits the raw key altogether, not `raw: null`.
2. When null and format=None, preserve legacy raw behavior: true for Workout/Routine/HistoryEntry, absent for WorkoutSummary.
3. When null and an explicit format is selected, default true only for individual full get_workout/get_routine; default false for lists/history and all summaries.
4. Explicit include_raw=true on a summary attaches that item's original raw object when available. On list_workouts this requires detail hydration because WorkoutSummary does not carry raw; document and test the at-most-50 extra GETs. Do not attach an unrelated page envelope as an item's raw.
5. Writes keep their legacy full normalized/raw output; no P arguments are added to mutations.

This resolves the brief's “raw on for detail/off for lists” goal for the new explicit read modes without silently deleting raw from legacy recent/routine/history calls. Examples: `get_recent_workouts()` is unchanged; `get_recent_workouts(format="full")` includes every normalized set with no raw; `get_workout(include_raw=false)` keeps all normalized detail.

Summary projection validates the source at its original completeness level first. A compact list summary is allowed to lack sets; an alleged complete workout is not. Whole-call error on hydration failure: do not drop an item, advance a cursor past an undelivered item or return summaries under a full schema.

### 5.2 A2: checkpoints and finite scanning

The legacy adapter translates public pages to raw offsets, `(page - 1) * page_size`, then batches. Workout requests send limit=min(public limit, 5); history's old path requests its public limit and aggregates the upstream's five-row responses. It may fetch a tail beyond the public limit and slice it. Preserve these exact old request tests.

New scanning is a separate path, activated by dates, cursor, include_meta=true or nondefault scan_limit; B5 always uses it. P-only projections and include_stats-only A3 calls do not need a new scan. Its low-level calls use raw offsets and at most **five** rows per batch, including history. Do not combine the legacy adapter's internal aggregation with a second scanner that guesses consumed offsets.

Scanner contract:

- Source offset starts at (page-1)*page_size for list/history, zero for recent/stats, or the validated cursor's next_offset.
- A cursor may accompany only page=1/default. Cursor values control the actual source offset. Changing source kind, exercise ID, date window or page_size/limit invalidates it; display/raw/stat options do not.
- Each request asks for min(5, remaining output capacity, remaining scan rows). Consume every returned row, validate it, apply date filtering, deduplicate within this invocation and advance the raw offset by the number consumed. Do not slice away a hidden matching tail.
- Return at most page_size/limit eligible rows. With filters, several source batches can produce one public result. Exhaustion means a short batch relative to the actual requested size (including an empty batch), not a filtered-empty result.
- Public scan_limit defaults to 200 and is 1..200 source rows per invocation. Also cap at 40 distinct source-batch requests and a 120-second monotonic scanner budget. Read retries consume time and wire attempts; no batch is begun after the budget expires. Per-request timeout is at most the remaining budget.
- These are wrapper policy limits, not alleged Hevy limits. The existing legacy path is not given a new truncation policy.
- No early exit merely because one row is older/newer than a date boundary: global upstream ordering is not freshly proven. No unbounded history walk.
- Unexpected list, detail or paging schema remains an error. Budget exhaustion returns bounded usable rows with explicit incomplete scope; a malformed/failed batch produces E instead of a false complete empty result. It may expose a safe restart offset in error context, never a continuation after undelivered data.

Checkpoint metadata model:

| Field | Contract |
|---|---|
| next_offset | int>=0 or null; next unconsumed source row, null only when source exhaustion was observed |
| next_page | int>=1 or null; next_offset/page_size + 1 only when exactly aligned; otherwise use cursor |
| next_cursor | opaque string or null; explicit source offset and scope, never a token or raw workout |
| has_more | bool; **conservative continuation signal**: true until a short/empty source batch establishes exhaustion. A full last page can therefore produce one empty terminal continuation. Not a guaranteed additional matching row. |
| total_seen | Actual source rows consumed in this continuation chain, including filtered/duplicate rows; starts at zero for a fresh page=N request, not a fabricated count of preceding pages |
| scanned_this_call | Actual source rows consumed in this call |
| returned_count | Number of delivered items |
| source_exhausted | bool; only based on the source batch termination rule |
| scan_complete | bool; requested output capacity reached or source exhausted before a budget stop |
| truncated | bool; a row/batch/time budget prevented filling the requested output; does not mean remote data was lost |
| window_applied | "none" or "client" |
| since / until | Normalized UTC strings or null |
| consistency | Literal "best_effort_offset"; upstream list can change between calls |
| stop_reason | "output_limit" / "source_exhausted" / "scan_limit" / "batch_limit" / "deadline" |
| page_count_authoritative | Literal false; legacy page_count is retained but not navigation authority |

Expose metadata always in protocol `_meta.hevy` for scanner reads. `include_meta=true` additionally includes these fields in JSON:

- list_workouts retains `items/page/page_count` and adds checkpoint fields at the page root.
- Array tools use `{items: […], …checkpoint fields}` **only when include_meta=true**.
- With include_meta=false, bare-list output keeps one JSON text block per item (zero for empty), and structured content stays `{result: […]}`; metadata remains in `_meta.hevy`.
- list_routines has no cursor: its optional envelope is `{items, returned_count, source_exhausted: true, window_applied: "none"}` after the complete sync envelope check.
- A1 alone does not create scan metadata or an envelope. Checkpoint properties are optional in schemas for unscanned legacy paths.

Cursor v1 is a size-bounded (<=2 KiB), URL-safe encoded typed document containing version, source kind, next_offset, total_seen, normalized query scope (exercise ID when applicable, since/until and page_size/limit) and its SHA-256 fingerprint. Reject an oversized scope before scanner IO. Validate every field/range, version and fingerprint; reject malformed/mismatched scope as INVALID_ARGUMENT. The hash detects scope mismatch, not authentication. Treat cursor content as untrusted input; it cannot select URLs, accounts, routes or a larger budget. No embedded raw rows, credentials or unbounded ID list. Tool continuations require the same query/size; the continuation resource can recover those validated values from the cursor itself. Cursors survive process restart but are only supported for their version. next_page is provided for page-based list/history tools only; recent/stats continue through next_cursor.

Do not claim snapshot isolation or cross-call deduplication from a stateless offset cursor. Insertions/deletions can shift offsets; callers can deduplicate returned IDs. Within a stable dataset a cursor resumes exactly after consumed rows, including a non-page-aligned boundary. Example: 10 rows scanned, 3 in-window matches, scan limit reached → next_offset=10, total_seen=10, has_more=true, truncated=true, regardless of the legacy page_count=1. Zero in-window rows with has_more=true is not proof of no matching history.

### 5.3 A3: deterministic statistics over the same data

`get_exercise_history(..., include_stats=true)` adds `stats: SetStatistics` per HistoryEntry/HistorySummary, without changing sets or making another upstream call. False is the compatibility default. When include_meta=true, the envelope also contains aggregate `stats` over its returned entries and a scope record. Otherwise aggregate scope is available in `_meta.hevy.stats_scope`; do not invent a non-array JSON root.

SetStatistics fields:

- `algorithm_version: "hevy-local-v1"`, `set_count`, `working_set_count`, `eligible_strength_sets`, `excluded_warmup_sets`, `invalid_or_missing_load_sets`.
- `total_volume_kg: float>=0`: sum weight_kg × reps over non-warmup sets with finite nonnegative weight and integer reps>=0. Missing or invalid load/reps is excluded and counted; do not reinterpret it as zero.
- `best_e1rm: RecordSet|null`, `heaviest_set: RecordSet|null`, `max_reps_set: RecordSet|null`.
- `rep_zones: {one_to_five: int, six_to_twelve: int, thirteen_plus: int, zero: int, unknown: int}`, counted by non-warmup set, not repetitions.
- `warnings: list[str]` containing fixed identifiers, e.g. `estimated_not_measured`, `external_load_only`, `high_rep_estimate_excluded`, `missing_load`. No arbitrary upstream text.

RecordSet is `{workout_id, exercise_template_id, started_at, set_id: str|null, set_index: int>=0, weight_kg: float|null, reps: int|null, e1rm_kg: float|null}`. Retain source IDs and zero-based source index even when set_id is absent.

Rules are local design choices:

- Working sets include normal/dropset/failure and exclude warmup. Never filter raw history entries out of the response because their sets are ineligible for a metric.
- Epley estimate: for positive finite weight and 1..12 reps, e1RM=weight for one rep; otherwise weight×(1+reps/30). Exclude zero/negative/missing loads and >12 reps from e1RM. Round outputs to two decimals after selecting on unrounded values.
- Heaviest set requires positive finite load and reps>=1; max-reps requires reps>=1 and can retain a null load. No assumed bodyweight, assistance adjustment, side multiplier or exercise-specific conversion.
- Ties: larger metric, then more recent started_at, then lexicographic workout_id, then lower set_index. Max-reps ties use weight (missing below any known load) before date; heaviest-set ties use reps before date. Fully identical observations keep stable input order.
- Empty/ineligible history yields null records and zero relevant totals; it is not an error. Statistics remain estimates scoped to supplied rows.

Unknown/negative response weights can survive historical normalization but do not enter load statistics. RecordSet uses null load for missing/nonfinite/negative values; never emit nonfinite analysis numbers. invalid_or_missing_load_sets counts working sets with missing/nonfinite/negative load or missing reps; zero load is valid for volume but ineligible for strength records. The pure reducer counts the supplied observations, including repeats; it does not silently rewrite legacy history or deduplicate distinct same-load sets. New scanner paths deduplicate repeated history rows within an invocation by workout_id plus exercise_template_id, keeping the first validated row. Callers combining batches must deduplicate observations themselves. Tests cover these rules, warmups, dropsets, zero/missing reps/load, high reps and deterministic ties.

### 5.4 A4: real date windows on free-tier reads

The MCP v1 signatures had **no since/until arguments**. Only HevyService.list_workouts/get_exercise_history exposed them, validated them and passed them toward an adapter that drops unsupported workout filters. V2 adds optional MCP since/until on list_workouts, get_recent_workouts and get_exercise_history, and on B5. This is additive; do not claim those arguments existed on v1 MCP.

Normalize using the copied timestamp function/validation rules: ISO dates and timestamps accepted, naive values mean UTC, null/empty string means no bound, second precision, since<=until. Window membership is inclusive `since <= started_at <= until`; missing bounds are open. Filter by session start, not end, creation or update time. Reject invalid/reversed dates before IO.

Apply the window **after validating each source row and before output selection**, through §5.2 batching. Never forward since/until to workout or new history scanner calls. Existing direct service behavior remains untouched for parity; the facade implements the upgrade. get_workout/get_routine do not gain date windows; routine models have no session-start time.

Return window_applied="client", normalized bounds, scan extent and continuation whenever a window is used. Results are complete only within the scanned scope until source_exhausted; don't promise access to data the normal session's endpoint does not supply or a bypass of every free-tier historical restriction. Date filtering is functional even when the requested window is older than the first batch, with honest truncation instead of a false empty conclusion.

### 5.5 C1: agent-first descriptions and instructions

Every description states purpose, limits and the next useful call; no implementation jargon or implied Pro key. Preserve the existing deletion warning's substance and explicit-user-request rule. Test the following content, not exact editorial punctuation:

| Tool / group | Required description content |
|---|---|
| list_workouts | List IDs and compact sessions; page_size<=50; checkpoint for continuation; get_workout for sets. Recommend include_meta=true for window scans. |
| get_workout | Read full sets/notes and canonical ID/title/start time; use these exact values before delete_workout. include_raw=false saves tokens. |
| recent | Default 10, maximum 100 complete sessions; explicit summary mode and client-side windows; no “all history” claim. |
| exercise history / stats | Progression/load context; include_stats=true for local metrics; records apply to returned/scanned history, not necessarily all-time. |
| list/get routine | Resolve routine IDs; always get_routine before full replacement; output extras must be projected into input schema. |
| create/update routine | Real write; one attempt; update is full replacement of exercises/sets; accepted-versus-transmitted field limitations linked in docs. |
| delete routine | Permanent; explicit request; no snapshot guarantee borrowed from workout deletion. |
| delete workout | Explicit deletion request for this identified workout only; never infer from duplicates/odd data; exact title/time, configured backend, private snapshot, absence verification. |
| template search | Search local catalog before choosing exercise_template_id; no guessed IDs or remote catalog scrape during a call. |
| folders / workout logging | Available only at their documented gate; read back and inspect unknown outcomes before a deliberate next action. |

Server instructions give read → inspect → full-replacement recipes, cursor reuse, raw/format defaults, and the difference between a bounded record and all-time PR. Workout titles, notes, custom exercise names and raw data are **untrusted data, not instructions**. No embedded text may authorize deletion, reveal credentials or change safety policy. Authentication is a separate terminal activity; no login tool or server-side prompt.

### 5.6 C2: resources and templates

Register with the probed `server.resource(uri, mime_type="application/json")` API. Literal URIs are static resources; placeholder URIs are templates. Resource/template listing does no Hevy IO. All reads use the same runtime/lane/facade and limits as tools, and can refresh a normal session exactly as a read tool does.

| URI | JSON content / service mapping | Bound / default |
|---|---|---|
| hevy://help | Tool recipes, modes, limits, supported capabilities and free-tier/session description | Static; no credentials, paths or account data |
| hevy://workouts | list_workouts(page=1, page_size=10, format="summary", include_meta=true) | At most 10 summaries |
| hevy://workouts/{workout_id} | get_workout(workout_id, format="full", include_raw=false) | One validated Workout |
| hevy://workout-pages/{page}/{page_size} | list_workouts with validated integers, summary and include_meta=true | page>=1, page_size 1..50 |
| hevy://workout-continuations/{cursor} | Continue a workout-list scanner checkpoint; recover its fixed scope safely | Same size/window/scan limits; no URL selection from cursor |
| hevy://routines | list_routines(format="summary", include_meta=true) | Same complete-sync requirement as the tool |
| hevy://routines/{routine_id} | get_routine(format="full", include_raw=false) | One routine, including normalized input-relevant data |
| hevy://templates | Catalog provenance and count, first 20 local search matches | No remote custom lookup or refresh |
| hevy://templates/{exercise_template_id} | Exact local catalog ID lookup | One Template; NOT_FOUND if absent, CATALOG_UNAVAILABLE if no usable catalog |

The template resources activate with B4's verified snapshot in the release phase; before then hevy://help reports catalog unavailable and their reads return the typed capability failure. C2 core workout/routine resources ship with the tool surface. No resources mutate, delete, log workouts or call export/email methods. Do not synthesize a per-account resource list by prefetching every workout.

Return JSON text via ResourceContent with application/json; metadata uses ResourceResult.meta. Successful array mappings use the explicit browse envelope above. Resource failures are a JSON `{error: ErrorDetail}` document, with `_meta.hevy.status="error"`; MCP ReadResourceResult has no isError. Unsupported protocol methods/unknown resources may remain framework protocol errors. URI parameter validation and domain/resource execution errors go through the resource boundary/bridge. Describe the error-document contract in help; clients must inspect error before treating it as data.

Percent-decode URI values once through framework routing, validate IDs and cursor bounds, reject traversal/control/embedded URI syntax, and parse numeric bounds explicitly. Never use resource input as a filesystem path or arbitrary URL. Avoid notifications/subscriptions or long-lived resource caches in this release. All resource/tool schema and failure behavior must be exercised over stdio.


## 6. Additive B tool contracts and evidence gates

### 6.1 B4: catalog-backed exercise template search

**Status:** method/parser surface verified-source; the public search contract below is a local design decision. A redistributable built-in catalog snapshot with provenance must be supplied during implementation. It is not present in the installed wheel. Do not fabricate a production catalog from synthetic test fixtures.

Inputs are exactly the table in §4.1. query length is 0..200; empty/whitespace query means browse. Trim/casefold query and collapse whitespace; split into tokens, require every token as a substring of the normalized title. Do not fuzzy-pick a single ID or transliterate names. muscle_group and equipment_category, when supplied, are nonblank exact casefolded filters on the corresponding catalog field; unknown filter value returns zero matches. Match only title, not instructions/media text. Deterministic sort is casefolded title then ID, with exact whole-title matches first; filter/sort before offset/limit.

Template fields:

- `exercise_template_id: str` mapped from parser id; `title: str`, `muscle_group: str` required/nonblank.
- `equipment_category: str|null=null`, `exercise_type: str|null=null`, `other_muscles: list[str]=[]`.
- `is_custom: bool=false`, `source: "bundled"|"local_cache"|"custom"`.
- `units: dict[str, str]|null=null`; do not derive kg/reps capability merely from title or exercise_type. list_template_units exists, but its response mapping needs evidence before use.
- `url/thumbnail_url/media_type: str|null=null`, `priority: int|null=null`, preserving the parser's available fields. No server download/embedding of those URLs.
- `raw: object` only when include_raw=true. The parser field set was verified in exercise_catalog.parse_exercises_from_app_js; live custom-template bodies are asserted until fixtures establish them.

TemplateSearchResult is:

- `items: list[Template]`, `query: str`, `muscle_group/equipment_category: str|null`.
- `offset: int`, `limit: int`, `matched_count: int` (exact for the loaded sources), `returned_count: int`, `next_offset: int|null`, `has_more: bool`.
- `catalog: {version: str, checksum: str, source_url: str|null, captured_at: UTC string|null, loaded_from: "bundled"|"local_cache", stale: bool, source_count: int}`.
- `custom_included: bool`, `custom_cache_age_seconds: float|null`, `upstream_complete: bool|null` (null unless source completeness is established).
- No claimed total exercise universe, current catalog freshness or account ownership from a public exercise ID.

Local loading preference: validated account-independent snapshot in HEVY_CONFIG_DIR if explicitly maintained there, otherwise package data. Cache files use private atomic storage; provenance/checksum must match. Corrupt optional override falls back to a valid bundled snapshot with a fixed warning; if neither works, CATALOG_UNAVAILABLE. Keep a process-memory immutable index keyed by checksum; no timed network refresh.

include_custom=false performs **zero upstream calls**, needs no session and bypasses the account lane. include_custom=true merges a verified custom-template list, from a private per-account memory cache for five minutes or one authenticated GET /custom_exercise_templates (bounded read retry policy). Key by stable account identity, not a literal token; invalidate on logout/account change. Account-specific templates override equal IDs, never overwrite an unrelated ID merely because titles match. Do not persist custom definitions into shared package data. A custom list/auth/schema failure yields E, rather than quietly reporting public matches as the requested complete result. The custom envelope/schema must be verified before exposing that option as supported; otherwise return CAPABILITY_UNAVAILABLE before IO.

Production snapshot acquisition belongs to the release evidence: use a reviewed already available public web-app bundle/catalog export, the pinned parser, a source URL/date/checksum and redistribution attribution. An offline maintenance script can parse a supplied local file; neither builds, tests, startup nor MCP searches download a catalog. If new public app-bundle capture is needed later, it is a separate documented maintenance action with bounded requests, never a guessed remote-search fallback. No custom-template CRUD is hidden in B4.

### 6.2 B5: bounded exercise statistics / records

**Status:** history client/normalization verified-source; calculations and public result are asserted local design, not an upstream PR API.

get_exercise_stats validates the same template/date rules and uses the scanner with output limit default 100, maximum 1000, scan_limit default/maximum 200 per invocation. It can return fewer entries because of the scan/time budget. One invocation never follows its cursor recursively. Subsequent calls are explicit; results are scoped batches which the caller may combine by IDs. There is no claim of a complete local historical database.

ExerciseStatsResult fields:

- `exercise_template_id: str`, `stats: SetStatistics` using exactly the A3 algorithm.
- `scope: {kind: "returned_history", since: UTC string|null, until: UTC string|null, entries: int, sets: int, oldest_started_at: UTC string|null, newest_started_at: UTC string|null, started_from_origin: bool, source_exhausted: bool, all_accessible_history_included: bool, upstream_history_complete: bool|null}`.
- `records_are_all_time: bool`: false unless origin was scanned without a date filter to observed source exhaustion in this same aggregate **and** upstream history completeness has separate evidence. Default false, upstream_history_complete=null.
- All §5.2 checkpoint fields, including returned_count equal to history observations included, not the number of record metrics.
- `history: list[HistoryEntry]` only when include_raw=true; this opt-in carries the normalized observations with raw for audit/merging. Otherwise omit history completely. RecordSet retains recovery/source IDs even without this field.

all_accessible_history_included describes traversal of what the session endpoint supplied, not the absence of remote paywall/retention restrictions. It is false for a continuation batch unless a future explicit aggregate spans the origin; cursor total_seen is not a statistics accumulator. Do not sum best e1RM, percentages, duplicate volume or average averages across cursors. A future local-store aggregate needs a separate data-sync design; get_batch alone does not establish one.

No profile/metrics/export API, Pro key, write or additional upstream request per metric. An empty bounded scope produces null records and zero volume with honest completeness. C1 tells the trainer to use these as records within scope, never a diagnosis, training prescription or claimed measured 1RM.

### 6.3 Minimal B3: folder list/create, conditional on evidence

**Status:** routes/wrappers verified-source; field/response shapes **asserted**, not live-proven. Include these tools if the release phase can close their shape evidence without displacing required A/C/B4/B5 work. Until then, absent from default tools/list. This implements the October promotion without pretending all folder CRUD is committed.

RoutineFolder is `{id: str, title: str, index: int=0}`. Normalize numeric/string IDs to nonblank strings suitable for RoutineInput.folder_id. Preserve unmodeled response fields internally, not in the compact public model. The proposed list decoder accepts a raw array of folder objects; any wrapper alternative must be separately evidenced, not recursively guessed. Non-list/missing IDs/title is SCHEMA_ERROR. list_routine_folders takes no arguments and returns an array, no invented page count.

create_routine_folder requires title min_length=1 (do not silently trim transmitted text) and index>=0, default 0. Proposed inner folder body is `{title, index}`; **these body fields remain asserted**. The SDK-proven wrapper is `{"folder": inner}` with its existing default send_sync_event_to_mobile_app=true query. Do not borrow routine create UUID fields absent folder evidence. One POST only.

Success requires an evidenced response ID, then list_folders and locate the exact ID with matching title/index. Proposed response decoders (direct folder or folderId-bearing object) must be fixed from a sanitized fixture before registration; there is no title-only identity recovery, because duplicate names can exist. Unknown created ID, failed read-back or ambiguous POST is a typed partial outcome with phase/known ID, never another POST. Existing-folder lookup can help a human inspect state but is not proof of which creation succeeded.

Acceptance needs offline request/envelope tests and a separately authorized disposable folder create→list verification record before enabling its mutator. Cleanup uses an evidenced, manually selected folder path; optional folder deletion is not exposed as a tool just to clean up. Folder update/delete/reorder and routine movement remain sketches until their semantics, especially contained-routine deletion/movement, are known.

### 6.4 B1: workout logging, gated

**Introspection result: unavailable.** WorkoutsAPI has no create/update methods; it is read plus comment/like mutation, not a session-logging API. RawHevyClient is a generic transport, not evidence that a particular workout route/body is supported. Either recover the actual web-app routes with authorized evidence or deliberately evaluate an upstream version adding typed support. Never upgrade the pin or implement guessed POST/PUT endpoints in a parity release.

Proposed public models below are **asserted**. They define intended agent behavior while transport remains unavailable:

- WorkoutInput: extra=forbid, required `title: str(min_length=1)`, `started_at: ISO string`, `exercises: list[WorkoutExerciseInput]`; optional `ended_at: ISO string|null=null`, `notes: str|null=null`, `routine_id: str|null=null`. Normalize UTC, ended_at>=started_at when present. No fabricated timestamps and no server-generated “now” default.
- WorkoutExerciseInput: extra=forbid, required nonblank exercise_template_id; optional `id: str|null=null, notes: str|null=null, superset_id: int|null=null, rest_seconds: int>=0|null=null`; required `sets: list[WorkoutSetInput]`. Order is meaningful.
- WorkoutSetInput: extra=forbid, `id: str|null=null`, `type="normal"` from the existing four types; optional nonnegative weight_kg/distance_meters, reps/duration_seconds; optional rpe 0..10, notes; missing measurements remain null. At least one of reps/distance/duration must be present; distinguish actual performed sets from routine prescription DTOs.
- No arbitrary raw/body field, API key, URL, retry flag, implicit delete or template creation.

Any adjustment required by recovered remote semantics must be documented as a B1 contract decision before enabling, with both public and wire models tested. In particular, accepted RPE/set types, optional IDs and superset naming must be evidenced. Do not assume the routine translator is a valid workout translator or silently discard logging fields.

create_workout: validate all input before IO; generate an operation/client UUID only if the observed endpoint supports it, without claiming idempotency. Exactly one wire mutation. Obtain a verified ID, singular GET, validate identity and intended session start/title and performed exercise/set measurements according to the recovered contract. Success is the normal Workout JSON root. Unknown ID/acknowledgment or failed verification produces a typed partial/unknown outcome. No automatic compensating delete.

update_workout: get the complete existing session first; caller intent covers **full replacement**, including omitted exercises/sets. Require existing ID ownership/read-back evidence; persist a private pre-update normalized+raw snapshot using the atomic storage primitives in a separate updated-workouts archive before the one update attempt. A new MutationSnapshotStore records operation="update", schema version and target identity; do not mislabel it as a deletion backend. Singular GET verifies identity and intended persisted fields. Success is Workout, with snapshot provenance in metadata; failure retains snapshot/known target and never restores or retries automatically.

The existing delete_workout is B1's delete operation too. Its signature, configured backend, identity/snapshot guarantees and reconciliation remain unchanged. Creating a workout does not prove a new delete backend.

Enablement requires all of:

1. Sanitized method/path/header/body/response fixtures from the actual normal-session web flow, or a pinned upstream implementation with the same evidence.
2. Offline create/update validation, one-attempt failure matrix, read-back mismatch tests and snapshot/cancellation coverage.
3. A dedicated later live record: explicitly authorized disposable session with unique test title, create once → get/verify → update once → get/verify → guarded delete once → verify absent. Use the existing proven configured delete backend for self-cleanup.
4. Record phase outcomes, safe IDs/shape summaries and cleanup status without credentials. If any phase is unknown, inspect first; do not recreate to “try again”. Cleanup failure leaves an explicit known target for manual cleanup.
5. Capability gate and enable_workout_logging flag both pass. Before then there are no advertised workout-write tools, even if a flag is set.

If no configured deletion backend is proven, prepare a manually created disposable session and complete test-delete-backend first. No shotgun candidate probing and no creation-first experiment whose safe cleanup path is unknown. A release does not wait for the unavailable B1; workout logging itself stays incomplete until this gate passes.

### 6.5 Uncommitted extensions

B2 measurements need kind/unit/timestamp vocabulary, request/response methods and duplicate/verification behavior. Their sketch uses typed values and one write attempt, but no API route or SDK method is asserted as available.

Remaining B3 needs folder identity, ordering and contained-routine semantics. Custom-template writes need their own strict input/read-back contracts. The verified SDK methods justify future investigation, not automatic registration.

A future export CLI could request an email link and download an account export with a user-directed destination. email_download_link is a side effect; presigned_url may be for upload rather than export. Do not add an export command, arbitrary URL fetch or background backup in this port. Likewise, do not infer incremental sync from get_batch or community mobile documentation.


## 7. Stable errors, typed outcomes and FastMCP boundary

### 7.1 Wire models and schema publication

Publish explicit Pydantic-backed **object** output schemas. Each includes success, Failure and, for writes, PartialMutationFailure alternatives. Do not claim a bare ToolResult annotation generates these models automatically; `output_schema=` must be supplied.

- Object success keeps its historical fields/root.
- Bare-list success explicitly emits one JSON TextContent block per item, zero blocks for an empty list, and structured content `{result: array}`, matching the old SDK wire behavior. The published structured schema describes that wrapper. Explicit include_meta=true instead returns the documented items envelope in one JSON block and structured content.
- Object results have one JSON text block. Decode object text or collect list-item text values to compare with structured content; whitespace is not a compatibility guarantee. Metadata is protocol _meta. Never rely on FastMCP's automatic list conversion for legacy calls.
- New response models forbid unexpected fields except intentionally permissive historical SetData/ExerciseData/raw objects. A projection must preserve historical response extras in full mode.
- Failure JSON text and structured content are `{error: ErrorDetail}`; partial writes are `{error: ErrorDetail, outcome: MutationOutcome}`. Both use ToolResult.is_error=true so wire isError is true. Do not put a failure inside a successful result or discard a snapshot path.

ErrorDetail fields, all first four required:

| Field | Type / contract |
|---|---|
| code | Closed stable Literal vocabulary in §7.2 |
| message | Safe fixed/local summary, never str(upstream exception) |
| hint | Concrete next action appropriate to phase; never instruct automatic mutation replay |
| retryable | bool; true only for a later deliberate read attempt after a transient condition |
| retry_after_seconds | Optional finite nonnegative number when known; omit when not evidenced, do not fabricate |
| context | Optional typed allowlist: tool, phase, correlation_id, resource_id, field_names, cause_code; no request body, headers, tokens or arbitrary exception attributes |

A schema composition uses a top-level type=object and alternatives with distinguishing required fields, carrying model definitions correctly. Use anyOf if compatibility/projection variants overlap; do not publish an invalid disjoint oneOf for overlapping variants. JSON Schema tests must validate actual success, empty, failure and partial payloads against the emitted schema, including strict nested routine inputs.

PartialMutationFailure is a failure with useful typed state, not a guessed success. MutationOutcome fields:

- `operation: create|update|delete`, `resource_type: routine|workout|routine_folder`, `resource_id: str|null`.
- `phase: validation|session|preflight|snapshot|mutation|response|verification`.
- `request_state: not_attempted|may_have_been_sent|acknowledged`; acknowledged means a usable HTTP response was received, not proof of every intended effect.
- `effect: not_applied|applied|unknown|still_present`; only claim not_applied for local rejection/clear pre-attempt failure, and applied for verified behavior.
- `verification: not_run|verified|absent|present|mismatch|inconclusive`.
- `snapshot_path: str|null`, `backend: str|null`, `mutation_attempts: int[0..1]` when known, `automatic_replay: Literal[false]`.
- `cause_code: stable code|null` for the underlying read-back or attempt error.

Known target/snapshot/backend must survive even when verification fails. Null means unknown, not absent. A malformed acknowledged response with no usable ID is unknown, even if the underlying error is SCHEMA_ERROR. Do not find a routine by title and assert it is the created one.

### 7.2 Exception-to-code mapping

Preserve all **15** exception classes and their inheritance. Choose the most specific class first, particularly BackendDriftError before EndpointChangedError. Codes below are the stable v2 boundary vocabulary; direct service exceptions and old tests remain unchanged.

| Exception / condition | Stable code | Default retryable for reads | Safe hint |
|---|---|---:|---|
| HevyError (otherwise unclassified) | HEVY_ERROR | false | Inspect the operation/configuration and current upstream compatibility; do not repeat a write blindly. |
| AuthenticationError | AUTHENTICATION_ERROR | false | Run hevy-mcp auth status; if needed auth login separately, then inspect the target before any repeated mutation. |
| AuthorizationError | AUTHORIZATION_ERROR | false | Check account permission/session and target ownership; login is not a guaranteed fix for 403. |
| NotFoundError | NOT_FOUND | false | Verify the exact ID or whether the object was removed. |
| ConflictError | CONFLICT | false | Read the current target and reconcile intent before submitting again. |
| MutationIndeterminateError | MUTATION_INDETERMINATE | false | Inspect the known target/snapshot; the request was not replayed. |
| RateLimitError | RATE_LIMITED | true | Wait for the known cooldown, otherwise wait and retry the read deliberately. |
| TransportError | TRANSPORT_ERROR | true | Check connectivity and retry the read later; inspect any possibly delivered write. |
| RetryableUpstreamError | UPSTREAM_UNAVAILABLE | true | Retry the read later after transient upstream failure. |
| SchemaError | SCHEMA_ERROR | false | Preserve sanitized shape evidence and update the adapter; malformed data is not an empty result. |
| EndpointChangedError | ENDPOINT_CHANGED | false | Review pinned endpoint behavior; do not switch write endpoints automatically. |
| BackendDriftError | BACKEND_DRIFT | false | Inspect the snapshot/target and verify this configured backend manually; no fallback. |
| IdentityMismatchError | IDENTITY_MISMATCH | false | Re-read canonical ID/title/start time or inspect mutation read-back; never alter expected identity just to force a delete. |
| SnapshotError | SNAPSHOT_ERROR | false | Fix private archive permissions/storage/fsync and inspect the file; no DELETE was sent when snapshot persistence failed. |
| ConfigurationError | CONFIGURATION_ERROR | false | Repair config; for missing workout backend use the guarded tester on a disposable session. |
| Caller/model argument validation, safe ValueError from input guards | INVALID_ARGUMENT | false | Correct named fields/ranges; never include the rejected raw input in the message. |
| Extension lacks verified capability | CAPABILITY_UNAVAILABLE | false | Use the supported surface or complete its documented evidence gate. |
| No valid offline catalog | CATALOG_UNAVAILABLE | false | Install/maintain the documented local catalog snapshot; do not make a remote-search guess. |
| Account lane admission timeout | BUSY | true | Wait for the active operation to finish, then retry the read deliberately. |
| Unexpected implementation/framework failure | INTERNAL_ERROR | false | Inspect sanitized logs by correlation ID; no bare traceback/body is returned. |

For **any mutation invocation**, set retryable=false, including rate limits and transient verification errors. retryable is informational, never an internal retry instruction. Before-attempt failures use the mapped code with request_state=not_attempted where useful. After a mutation may have been sent, attach MutationOutcome even when the surface code stays SCHEMA_ERROR/IDENTITY_MISMATCH/NOT_FOUND. Preserve cause_code; do not flatten fifteen meanings into “server error”.

The predecessor routine adapter already turns 401/429/5xx/transport failures during mutations into MutationIndeterminateError. Keep that. A read-back failure after an otherwise acknowledged create/update is a partial outcome; creation/update may have happened. An inconclusive routine-delete read-back cannot be reported deleted=true. The workout orchestrator's reconciliation rules remain authoritative (§8).

### 7.3 Boundary mechanics

Use a small `HevyToolError(ToolError)` bridge containing an already typed, sanitized Failure/PartialMutationFailure. Its message is a fixed safe summary, not the service exception text. A common async operation facade catches HevyError/known input errors before FastMCP wraps them, records phase/outcome and raises this bridge. It must also convert unexpected exceptions to a safe bridge while logging class/correlation only. Keep ordinary tool functions' explicit signatures; no decorator may hide them behind untyped *args/**kwargs.

The first on_call_tool middleware catches the bridge, emits ToolResult JSON/structured error, and catches FastMCP argument ValidationError separately as INVALID_ARGUMENT. Body/output validation failures are SCHEMA_ERROR/INTERNAL_ERROR as appropriate, not automatically blamed on caller input. Do not walk arbitrary exception chains or parse exception strings for codes. mask_error_details=true remains a final framework guard, not the domain error strategy.

The probed default rejects unknown function kwargs and emits additionalProperties=false; strict nested RoutineInput models retain their existing rules. Test this actual behavior rather than relying on the reference server's private precheck. Wrong MCP framing/unknown methods remain protocol errors. Do not rewrite initialization/notification failures into a fake tool response.

Resource wrappers use the same safe error classifier and phase recorder; on_read_resource emits ResourceResult with the typed error-document contract in §5.6. Resource argument decoding errors are safe INVALID_ARGUMENT documents. Domain results are generated from the same Pydantic definitions as tool results.

Unexpected exceptions can be logged by framework code before the outer middleware sees them. Expected errors therefore use the bridge; the shared formatter must additionally discard external message bodies and traceback rendering. Redacting just the returned ToolResult is insufficient.

### 7.4 Phase observation without changing the old state machines

An operation-scoped recorder is passed through runtime wrappers. An observed adapter/client facade marks entry to the actual synchronous mutation method and safely records recognized IDs/acknowledgment. A recording SnapshotStore delegate returns the original path and records it only after successful durable save. A delete-client delegate distinguishes preflight GET from post-attempt GET, preserving returned normalized objects/exceptions.

These wrappers call the old functions exactly once and never choose an alternative endpoint or initiate extra mutations. Do not parse messages to discover snapshot paths, locate “the newest” snapshot, infer whether bytes were delivered, or mutate exception inheritance to transport state. Worker observations must be thread-safe; ContextVars can carry operation identity through asyncio.to_thread, but mutable recorder updates still need synchronization.

If a request's wire state is uncertain, choose may_have_been_sent/unknown. Cancellation after dispatch cannot become not_attempted. Tests inject failures at every phase, including after a path was saved but before a usable result reached the caller.

## 8. Deletion and mutation guarantees

### 8.1 Workout deletion: unchanged authoritative sequence

1. Require a single configured backend. When unset, tool returns CONFIGURATION_ERROR and zero preflight/delete requests. Invalid configured values are safe configuration failures, not “try another backend”.
2. Validate workout_id against `[A-Za-z0-9_-]+` before GET, snapshot or DELETE.
3. Fetch the complete workout with normal-session singular get, not a list summary or Pro endpoint. Normalize title/time through the copied parser.
4. Compare returned ID and **exact canonical title** with supplied values. Normalize expected_started_at to UTC seconds and compare with the stored start. No fuzzy title matching, trimming, timezone guessing beyond the documented normalizer, or near-time tolerance.
5. Persist a complete normalized **and raw** snapshot successfully before any DELETE, independently of include_raw/summary presentation.
6. Attempt exactly one DELETE using exactly the configured backend and its exact headers.
7. Reconcile through a normal-session singular GET according to the table below; never replay DELETE or switch backend.

| Configured backend | Exact candidate path | Header behavior |
|---|---|---|
| internal | /workout/{id} | Preserve x-api-key=klean_hevy_mobile, hevy-app-version=2.5.6, hevy-app-build=1819922, hevy-platform=android 36 overrides |
| v1 | /v1/workouts/{id} | Raw client's session/web defaults |
| v2 | /v2/workouts/{id} | Raw client's session/web defaults |

These are **candidates**, not three independently live-proven deletion APIs. The internal x-api-key is an app identifier in existing source, not a Pro developer key. Preserve the exact request fixture and redact it like all API-key headers. SessionDeleteClient rejects every path outside the exact candidate set and closes its client in finally.

| DELETE / verification result | Required behavior |
|---|---|
| ID/title/start mismatch or invalid expected timestamp | IDENTITY_MISMATCH, zero DELETE; no snapshot on identity failure |
| Snapshot save/fsync/publication failure | SNAPSHOT_ERROR, zero DELETE |
| Successful response, then GET NotFound | Legacy DeletionResult, verified_absent=true |
| Authentication/transport/endpoint/schema failure from DELETE, then GET NotFound | Same success: observed absence reconciles even malformed 2xx/empty JSON |
| A reconciled attempt, then valid GET still returns workout | BACKEND_DRIFT with present outcome and snapshot; zero fallback/replay |
| A reconciled attempt, then GET fails inconclusively | MUTATION_INDETERMINATE with snapshot/backend/target, verification=inconclusive |
| Explicit BackendDriftError during DELETE | Propagate immediately as the copied orchestrator does; no invented extra verification/fallback |
| Other unexpected delete-client error | Copied orchestrator maps to BackendDriftError; do not broaden reconciliation in the port |

The last two branches matter: the old state machine does **not** verify after every possible exception. Carry its exact catch ordering over and expose what is known, rather than claiming a stronger guarantee than the code provides. A preflight NotFound is a failure for the requested delete, not a new deleted=true response. A later deliberate invocation is not an automatic retry and must revalidate identity/snapshot.

### 8.2 Snapshot storage contract

Default root is `~/.config/hevy-mcp/deleted-workouts/`, or HEVY_CONFIG_DIR/deleted-workouts. Directory 0700, JSON 0600. File name is UTC `YYYY-MM-DDTHH-MM-SSZ_<safe-id>.json`, with the sanitized ID truncated to 120 characters. Resolve containment before publication.

Payload remains exactly `{snapshot_version: 1, created_at, backend, workout: normalized Workout, raw: original raw}`. It contains personal session data, so never include its body in logs/evidence. Snapshot path is returned to the local caller, without a new promise about absolute-path formatting when HEVY_CONFIG_DIR itself is relative.

atomic_write creates a private temp file, writes/flushes/fsyncs, publishes with a no-overwrite hard link for exclusive snapshots, chmods and fsyncs the parent directory. Existing paths cannot be overwritten, including same-ID/same-second concurrent calls: one may succeed, others must fail closed before DELETE. “Immutable” here means no overwrite by this code, not an OS immutable bit or an undeletable archive. Keep the collision behavior; do not weaken it into os.replace.

A parent-directory fsync failure blocks deletion even if a published file remains. Cleanup temporary files as the original does. A snapshot is a recovery/evidence record, **not** a proven restore API. No auto-restore, upload or archive pruning.

### 8.3 Cancellation and complete-operation serialization

One async account lane covers session validation/refresh admission, scan/read work, routine mutation plus read-back, and the entire workout deletion sequence. Acquire within ten seconds or BUSY before beginning account work. Pure catalog/filter/stat computation on supplied data can bypass it. There is no unbounded mutation queue.

The runtime owns an operation task and shields/drains it while workers run. On caller cancellation, stop scheduling optional additional read work where safe; once a mutation has been dispatched, retain the lane until that attempt and its permitted reconciliation complete. A cancelled await on asyncio.to_thread does not stop its thread. Never release the lane, close the client beneath the thread or start another operation while the old mutation can still run.

No automatic follow-up mutation, compensating delete/restore or new retry is scheduled on cancellation. Record a sanitized outcome/correlation when the caller can no longer receive the typed response. Existing snapshots remain accessible. Shutdown waits for actual in-flight work to finish/close before runtime teardown. Test with barriers in fake workers and cancellation before dispatch, during wire call and during verification.

## 9. Transport, authentication, CLI and packaging

### 9.1 Exact attempt policy

| Path | Preserve / extend policy |
|---|---|
| PersistentUnofficialAdapter read actions | At most three adapter attempts total; first-attempt AuthenticationError can refresh once using expected_access_token. 429/5xx/transport read retries sleep 0.1 then 0.2 seconds. Every client closes. Username discovery and batched reads can be repeated as part of that bounded read action. |
| Persistent routine mutations | One synchronous SDK mutation invocation; SDK auto_refresh=false. 401/429/5xx/transport ambiguity is not refreshed/replayed as a write. Pre-attempt valid_session may refresh normally. Read-back has its own bounded read policy. |
| SessionDeleteClient singular GET | At most two attempts, only one first-401 refresh. Preserve this narrower policy; do not silently add generic transient retries. |
| RawHevyClient GET/HEAD | Default retries=2, thus at most three attempts for connection/timeout, 429 or >=500; copied sleep(0) hook. Terminal 5xx maps to EndpointChangedError in this raw path, unlike the SDK mapper's RetryableUpstreamError. |
| RawHevyClient mutations | Exactly one HTTP attempt regardless of retries setting; no 401 refresh/replay. Timeout 20 seconds, 204 returns None, malformed JSON is SchemaError. |
| New upgrade client actions | Reads reuse the same three-attempt/one-first-401 semantics, auto_refresh=false, shared session store. Mutations one attempt; never wrap a legacy adapter call in another retry loop. Scanner budgets account for attempts. |
| Catalog local search | Zero wire attempts unless include_custom=true; never network from list_catalog implicitly. |

The three-attempt bound is per adapter read action, not a promise that a paged tool issues at most three HTTP requests total. Each logical read can contain account discovery plus multiple batches. Log the actual route-template/attempt/phase; don't report an invented exactly-once network transaction.

Do not import Kleinanzeigen's pacing values as Hevy rate limits. The port preserves existing backoffs/status mapping. Future Retry-After/cooldown improvements need separate evidence/tests; do not claim the current raw client already honors that header. Emit retry_after_seconds only when known. No retries at MCP-client/tool-middleware level.

### 9.2 Token refresh and storage

Credentials remain `HEVY_CONFIG_DIR/credentials.json`, default `~/.config/hevy-mcp/credentials.json`; settings remain config.yaml with `hevy.workout_delete_backend`, including the old unwrapped YAML form on load. No copied tokens, new browser profile, alternate environment token or Pro key.

Session fields remain access_token, refresh_token, user_id/email nullable, expires_at nullable. Expiry check is the existing <=now comparison; no invented early-refresh window. Refresh holds the per-canonical-path asyncio lock, then runs the blocking refresh under `credentials.json.lock` with POSIX flock. Under flock it reloads disk state; if access_token differs from expected, reuse the rotated session instead of consuming another refresh token. Preserve identity/email fallback and atomic save; failure leaves original credentials byte-for-byte intact.

Lock file is 0600 with O_CLOEXEC/O_NOFOLLOW when supported; root 0700. Platforms without fcntl only have process-local coalescing: document that limitation, do not promise cross-process safety there. The operation lane is per runtime, while refresh locking coordinates independent processes sharing the store. Old/new servers should perform live account calls sequentially; flock serializes refresh, not arbitrary account mutations.

Browser login remains `login_via_browser(email=…, save=False)`; only our SessionManager persists tokens. No password input, duplicate library credential file, browser launch on startup or authentication MCP tool. Logout is local removal, idempotent; output remote_revoked=false. Missing/invalid credentials affect account operations, not tool/resource discovery or pure bundled-catalog search.

### 9.3 Redaction and protocol discipline

Use a single sanitized JSON stderr handler. Keep the copied recursive/text RedactionFilter, including **entire** Cookie/Set-Cookie/Authorization values through line end (Digest/Basic/multiple cookies included), token-key normalization, quoted multiline values, bearer tokens and secret query parameters. Redact all API-key headers, including static app identifiers.

New event fields are allowlisted: correlation_id, tool, phase, route template, status, elapsed_ms, attempt, error code. Avoid full request URLs/IDs unless needed in the typed caller outcome; no raw payloads, routine/workout notes, titles, email, credentials, headers, browser redirect URLs or local credential contents in logs. Snapshot contents never enter logs.

Route fastmcp/mcp/httpx/httpcore/Playwright-related logs through the same safe handler; remove their independent Rich handlers where installed. The formatter ignores external arbitrary messages and exception/stack text, emitting class/code/correlation or a fixed external_log event. It must not call a generic traceback formatter after the redaction filter. Test synthetic secret-bearing exceptions, nested request arguments and DEBUG mode. show_banner=false; every stdout line in serve is MCP JSON, with no CLI status/debug prose.

### 9.4 CLI parity

| Command | Flags / behavior to preserve |
|---|---|
| hevy-mcp serve | --log-level TEXT, default INFO; configure sanitized stderr then run stdio. No auth/read probe at startup. |
| hevy-mcp auth login | --email optional; open browser, save normal-session token atomically, print authenticated/user_id/email/expires_at only. |
| hevy-mcp auth status | Offline, no refresh/request. Print authenticated/user_id/email/expires_at/refreshable/expired; absent/corrupt session exits 1 with safe text. |
| hevy-mcp auth logout | Remove local credentials; authenticated=false, local_session_removed=true, remote_revoked=false; repeated invocation succeeds. |
| hevy-mcp doctor | Offline by default. Report config_dir/modes, credential presence/mode, session validity/expiry/refreshability, Playwright importability, Chromium executable availability, configured backend, live_request_performed=false. Existing --dry-run browser install check downloads nothing. |
| hevy-mcp doctor --live | Validate/refresh the session and make exactly one harmless service read get_recent_workouts(1), plus any read's required account lookup/retries. Add live_read_ok/user identity, exit nonzero if session/read fails. Never interpret “one read” as one physical request. |
| hevy-mcp test-delete-backend | Required --backend internal, v1 or v2, --workout-id, --title, --started-at. Validate candidate/title before construction, require title pattern `Disposable Delete Test <nonce>`, display identity and ask existing interactive confirmation. Use selected candidate even if settings unset; persist only after verified_absent=true. No auto-confirm, creation or probing all candidates. |

Preserve root --help, command exit behavior and `python -m hevy_mcp.cli`. CLI JSON may include identity/path/modes intentionally for local operations; it never prints tokens. Safe failure formatting may improve the old bare-exception paths but cannot remove verbs/flags or the tester confirmation. A FastMCP failure document is not blindly reused as a zero-exit CLI success.

Typer 0.17.4 + Click 8.3.3 resolved and the old CLI tests/help ran successfully in the 3.12 probe environment. Keep that pair pinned initially; don't upgrade one independently or assume all older Click/Typer combinations parse bool/required options the same way. Test --live, --email, --log-level and tester-required flags from an installed wheel on both Python versions.

### 9.5 Build, dependency and installation contract

Use `requires-python=">=3.12"`, hatchling (initially 1.27.0), wheel package `src/hevy_mcp`, distribution hevy-mcp and the existing console script/import path. Pin FastMCP 4.1.0 and retain MCP 2.0.0 for the demonstrated baseline; fastmcp-slim/mcp-types resolve compatibly at 4.1.0/2.0.0 in the inspected environment. Lock the complete graph with uv, don't copy the old lock wholesale or copy Kleinanzeigen's MCP 2.3.0 pin.

Keep hevy-unofficial[browser]==0.0.2a0 as an **external PyPI dependency**. The extra supplies Playwright; Chromium binaries are a separate explicit operator installation. Installed metadata also has a Playwright marker outside the core extra, so dropping [browser] is not a reliable “no browser dependency” strategy. No vendoring or install-time login.

Initial non-framework pins come from old-pyproject: httpx 0.28.1, pydantic 2.13.5, typer 0.17.4, pyyaml 6.0.2, click 8.3.3. Keep old strict ruff/mypy/test tooling versions unless the new graph requires an evidenced adjustment; add pytest-socket (0.8.1 was probed) and any required FastMCP type support explicitly. Ruff/mypy target py312. Build/lock setup may download packages; tests must not. Don't use uv run in an offline test command if it can trigger a missing-environment sync.

Implementation setup sequence: `uv lock`, `uv sync --locked --all-groups`, then direct `.venv/bin/python -m pytest` with network denied. With no network, prepopulate the wheel/cache environment and use `uv sync --locked --offline --all-groups`; a missing Playwright/package wheel is an installation failure to report, not permission to download during tests. Do not run `playwright install chromium` in CI/unit tests. Mock browser login and doctor browser checks; no browser binary is necessary for offline acceptance.

Wheel/sdist must include package data, docs, README, license/credits and required catalog provenance. Exclude .refs, credentials, config snapshots, deleted/updated workouts, logs, real HARs/fixtures, local .env, venvs and caches. Build then inspect archive contents; test imports, CLI, tools/resources and package data from an installed wheel outside the repo. No accidental import of the predecessor source via PYTHONPATH/editable fallback.

Use the guarded interpreter in OPERATIONS: `/usr/bin/env -u PYTHONPATH …`. Any future root launcher delegates to the same CLI; it is not necessary for console-script parity. Stdio is the transport requirement; no HTTP/OAuth hosting configuration is introduced by this design.


## 10. Offline verification and separate live evidence

### 10.1 Port the complete 151-case reference suite

Copy all eight test modules into tests with their cases/fixtures intact. Rewrite only framework-specific harness assumptions and assertions intentionally covering new additive behavior. A test count alone is insufficient: keep a parity matrix mapping each original test name/parametrization to its ported counterpart.

| Original module | Behavior that must remain covered |
|---|---|
| test_models.py | Timestamp/alias reconciliation, unknown nested fields, strict routine input, missing IDs/title/time, complete exercises/sets rejection |
| test_service.py | Compact/list versus detail parsing, live-shaped history, private DTOs/create IDs/update payload, CRUD/read-back ID checks, sync envelope/isMore, date/limit guards, malformed lists/pagination |
| test_unofficial_adapter.py | Authenticated username, page→offset, five-row aggregation, dropped workout date filters, history action mapping, status codes, no mutation replay, read retries and close-on-every-attempt |
| test_service_deletion.py | Identity/snapshot preflight, configured candidate, malformed-success reconciliation, present/inconclusive outcomes, zero fallback, unsafe IDs, exclusive publication and directory fsync |
| test_delete_client.py | Singular preflight GET, one read-401 refresh, cleanup, exact internal headers/candidate allowlist |
| test_raw_client.py | Bounded read retry, single mutation request even with high retry config, 401/timeout/JSON/status mapping |
| test_auth_config_logging.py | Refresh persistence/coalescing across managers/processes, failure byte preservation, browser save=false, missing browser safe failure, logout, browser extra and comprehensive secret redaction |
| test_server_cli.py | All ten signatures/safety hints, strict input schema, CLI/help/offline/live-doctor stub, tester config guards, stdio initialize/discovery/protocol stdout, README operations assertions |

“151” counts collected parametrized cases, not files. Preserve the complete source directory. In server tests replace SDK-private registry access with public list_tools/client calls. Run the ten-tool profile for exact historical inventory and the release profile for additive inventory. Do not edit tool-parity-contract.md to bless a regression.

### 10.2 Network isolation

pytest config defaults to `--disable-socket --allow-unix-socket`; include pytest-socket in the locked dev group. No allow-hosts exception for Hevy, localhost TCP, CDN or token endpoints. respx mocks/httpx.MockTransport and injected clients provide HTTP behavior. Unknown outbound requests fail the test. Use a temporary HEVY_CONFIG_DIR and fake credentials for every test, never the real home store.

pytest-socket is process-local. Child stdio/process-refresh tests must explicitly install the same guard in their bootstrap before imports, or run in a CI container/network namespace with egress disabled. A Python child harness can import pytest_socket and then run the module with runpy; production code does not gain a test backdoor. Wheel console-entry tests should run with OS/container egress disabled as well. Strip inherited PYTHONPATH and secrets; test harness paths must not load the predecessor accidentally. No Playwright browser, uv sync, package download or live fixture capture during tests.

AF_UNIX support is for event-loop plumbing, not authorization to connect to arbitrary local services. Test-created child processes must not invoke real browser/profile services. A deliberate socket-denial test should verify parent and child TCP connect fail, so a green suite cannot hide a missing child guard.

### 10.3 New tests with behavioral value

| Area | Required cases / acceptance evidence |
|---|---|
| FastMCP contracts | Public tools/list gives exact names/signatures/bounds/defaults/annotation aliases; no Context argument in JSON schema; root/structured array conventions; real result examples validate against explicit output schemas |
| Error boundary | Each of 15 service exceptions, argument errors, body schema validation, unexpected exceptions and bridge wrapping; no raw exception strings; all failure/partial results carry isError; read versus mutation retryable |
| Partial mutation state | Before attempt, acknowledged response without ID, post-write read-back 404/auth/schema/mismatch, known ID/path retained; no guessed successful create/delete or title-based recovery |
| Async runtime | Worker heartbeat while fake blocking IO runs, no worker loop assumption, concurrent tool/resource operations serialize; cancel with worker barrier retains lane; teardown waits/clients close; repeated lifecycle on same loop and fresh config roots |
| A1 | Every legacy success JSON including raw/null/default fields preserved; tri-state raw/default matrix, both summary/full forms; list raw hydration bounded and fail-closed; output projection never changes snapshots |
| A2 | Sizes 1/5/6/10/12/50 and history 100; zero/short/full terminal batches; no skipped tail with nonmultiple size; consumed offset/cursor/total_seen correct; full-page conservative has_more, invalid/mismatched cursor, changing dataset caveat |
| A4 | Window older than first page, inclusive equal bounds, date-only/offset UTC, empty/reversed/invalid inputs; filtered-empty nonterminal page; no since/until sent upstream; scan/row/time caps and restart position; no fabricated completeness |
| A3 / B5 | Known formula fixtures, single-rep, warmup/dropset, negative/nonfinite/missing/zero load, high reps, tie ordering, empty history, scope/duplicates, no extra calls for A3, false all-time claim on every incomplete/unknown source |
| B4 | Bundled/local-cache preference, provenance/checksum failure, deterministic tokens/filter/order/offset, zero IO/session for built-in search, missing snapshot error, no implicit list_catalog refresh; custom cache account separation/expiry and fail-closed merge |
| C1 / C2 | Safety/recipe description coverage, static discovery no IO, URI numeric/ID/cursor validation, JSON MIME, resource error documents/status metadata, equivalent service results, default bounded resource sizes |
| Conditional folders | Evidence-fixed list envelope/body, numeric/string ID normalization, one POST, duplicate titles, unknown ID/read-back mismatch and no replay; no implicit routine movement/deletion |
| Gated B1 | Capability/flag cannot register unsupported writes; later separate input/wire fixtures, performed-set translation, snapshot before replacement, one request and verification failure matrix |
| CLI / packaging | Every old verb/flag/exit semantics; mocked browser/doctor, tester decline/unverified result never persists config; wheel import/resource data outside repo; stderr secret/traceback suppression and pure stdout |

Do not replace service assertions with framework-only mocks. Use actual copied parsers/DTOs and respx where HTTP construction matters, and fake services only for framework isolation. Assertions on exact mutation counts and stored bytes are mandatory safety tests.

Run ruff check/format, mypy strict with py312, pytest on Python 3.12 and 3.13, then uv build and wheel/sdist inspection. Subprocess integration uses both FastMCP's in-process Client and SDK ClientSession/stdio_client with fake services. Actual CLI console script must be tested after wheel installation. No live calls in CI, release tests or default pytest collection.

### 10.4 Later smoke runner, outside pytest

Plan `scripts/smoke.py` with a reviewed SAFE allowlist: the six historical reads plus enabled search_exercise_templates/get_exercise_stats/list_routine_folders and bounded C2 resource reads. It must initialize over stdio, inspect annotations, call each selected read once serially with small limits and supplied/discovered IDs, compare schemas and report explicit SKIP for missing fixtures. No automatic create/update/delete, export/email, social or browser login. A skipped capability is not a full green acceptance run.

“One call” means one MCP invocation; normal read refresh/retries/account discovery can issue multiple HTTP requests. Use the existing credential store through its locking code; never record tokens, notes or full personal payloads. A safe read smoke may rotate a token; don't describe it as disk-side-effect-free.

Routine write checks and workout backend discovery are separate, user-involved disposable flows. The old README explicitly left live routine sync/history/write/deletion gates open; daily use/context does not automatically close every documented limitation. Carry unresolved gates forward and record current observations instead of claiming all candidates are proven.

Live verification records kept outside this repository capture date, pinned versions, tested capability, safe request/response shapes, result code, verified identity/absence and cleanup status. No real credentials or full workout snapshots. A read-only run cannot close write acceptance. B1 gets its own named record and enablement gate, not a footnote on parity smoke.

## 11. Implementation phases and acceptance

### 11.1 Foundation: this design

Deliver only docs/DESIGN.md. Framework/client inspection and reference-suite execution support the plan; source changes, package creation and live calls belong to later sessions.

Acceptance:

- [x] Numbered English blueprint includes every historical tool/default/model/annotation and all 15 exception classes.
- [x] A1–A4/C1–C2 are specified, B4/B5 planned, minimal folder amendment included conditionally, B1 explicitly gated.
- [x] Verified-source/probed/asserted/unavailable distinctions are explicit, especially workout writes and catalog bundling.
- [x] Only this document is left in the working tree; no commit or live server/config change.

### 11.2 Scaffold and core plumbing parity

Create pyproject/lock, src package, copied errors/config/auth/redaction, results/runtime skeleton, CLI shell and isolated test configuration. Keep framework changes small enough to review independently.

Acceptance:

- [ ] requires-python>=3.12, hevy_mcp import and hevy-mcp entry install on 3.12/3.13; no editable predecessor fallback.
- [ ] FastMCP/MCP/hevy-unofficial/browser/Typer/Click pins resolve; Chromium is not required for offline imports/tests.
- [ ] Auth/config/redaction reference cases pass, including distinct-process flock coalescing and byte-preserving refresh failure.
- [ ] Private files/directories/atomic writes retain exact permissions and publication semantics.
- [ ] pytest-socket denial reaches subprocesses; tests never read the real credential root.
- [ ] Error/partial schema definitions and bridge behavior pass a fake FastMCP call; stderr cannot render secret-bearing tracebacks.
- [ ] No login, upstream call or deletion during setup/test startup.

### 11.3 Verbatim models, service, adapters and deletion

Copy the source/test modules identified in §3.2. Wire default_service with the old normal-session adapter and optional deletion backend. Add observation delegates only where needed by the boundary; keep original method bodies intact.

Acceptance:

- [ ] Every non-server historical case passes; the 151-case parity matrix has no removed/weakened case.
- [ ] Exact private routine DTO/request tests pass, including accepted-but-untransmitted fields and matching create UUIDs.
- [ ] Five-row workout batching, history aggregation, username resolution and pagination/date guards match the reference.
- [ ] Reads have the exact old attempt limits; all mutation wire-count assertions are 0 or 1; every created client closes.
- [ ] Deletion snapshot has complete raw+normalized content even when future presentation omits raw.
- [ ] Identity mismatch, snapshot failure/fsync collision, malformed-success absence, still-present and inconclusive outcomes all match the original state machine.
- [ ] No new route, library auto-refresh, backend fallback or live test is introduced.

### 11.4 Tool surface: ten-tool parity plus the required upgrades

Replace SDK registration, implement explicit tool schemas/annotations, boundary and runtime lane; preserve CLI. Add required projection/checkpoint/window/stat options, agent descriptions and core resources. This phase includes complete A1–A4/C1–C2 functionality, with catalog resources returning a typed unavailable result until B4 data lands.

Acceptance:

- [ ] All 151 historical behaviors pass through their appropriate ported harness; base profile inventories exactly ten tools.
- [ ] Old calls return the exact legacy JSON success roots/fields/defaults/raw; optional modes have separately validated schemas.
- [ ] All annotation tuples, integer bounds and full-replacement semantics match tool-parity-contract.md.
- [ ] All 15 errors map stably; partial writes retain known identity/snapshot; isError and hints are tested over stdio.
- [ ] A1 raw/format matrix, A2 consumed-offset continuations, A3 pure metrics and A4 bounded local windows pass §10's edge cases.
- [ ] C1 recipes and C2 tool/resource equivalence, templates, MIME/errors/URI validation work; discovery performs no upstream IO.
- [ ] CLI subset/flags/exits/interactive tester unchanged; serve stdout is protocol-only at DEBUG as well as INFO.
- [ ] Worker/cancellation/lane/lifespan probes pass on Python 3.12 and 3.13; socket guard remains active.
- [ ] ruff, strict mypy, full offline suite and a synthetic stdio subprocess pass; no real account call.

### 11.5 Search, scoped records, docs and a distributable release

Implement B4/B5, acquire/validate a documented catalog snapshot, finish README/OPERATIONS, packaging and SAFE smoke scaffold. Minimal list/create folders can join only after their shape/verification gates fit the session; other B2/B3 sketches remain uncommitted.

Acceptance:

- [ ] search_exercise_templates works offline without credentials; package contains an evidenced real snapshot/provenance, not a synthetic mock or a missing library data assumption.
- [ ] No catalog auto-refresh/download at startup/search/build/test; custom option either has evidenced parsing or returns capability-unavailable explicitly.
- [ ] get_exercise_stats reuses A3 algorithm, bounds scanning, retains source records and never labels a truncated/unknown dataset all-time.
- [ ] Release profile has 12 tools; optional verified folder pair gives 14. Both coexist with the unchanged ten-tool profile.
- [ ] If folders included: fixed request/response fixtures, one POST, ID-based read-back and disposable live evidence before default enabling; otherwise record their concrete open gate, no fake success/stub registration.
- [ ] README leads with free Hevy tier/no Pro key, gives honest tools/modes/limits and unofficial-session limitations; OPERATIONS covers credentials/browser/deletion/backend candidates, read smoke, rollout and rollback.
- [ ] Docs explicitly cover five-row paging, wrapped GET routines, routine versus workout set fields, RPE input versus transmitted/observed semantics, and exact superset_id naming. Don't promote community mobile facts over existing fixtures.
- [ ] Wheel/sdist contain docs/data/attribution, exclude refs/secrets/snapshots; installed wheel CLI/tool/resource tests pass outside checkout.
- [ ] Complete offline checks pass; SAFE smoke runner is opt-in and excluded from pytest.

### 11.6 Parity audit and operational verification

Audit requirements against final schemas and tests, install into its own environment and register the server only after offline gates pass. Finish the live read and write evidence required for the capabilities being released, and do not repoint an existing registration until that evidence has been reviewed.

Acceptance:

- [ ] Signed-off parity matrix maps all 151 source cases and ten live-facing contracts; additive inventory and unavailable capabilities are documented.
- [ ] Read SAFE smoke is serial, bounded and fully reported; missing fixture/partial result is not silently green.
- [ ] Routine sync/history live limitations are resolved or explicitly retained as release blockers for the affected claims; routine disposable create/get/update/get/delete verifies actual shapes.
- [ ] A workout-delete candidate is enabled only through existing guarded tester and verified absence on one manually selected disposable session. No generic claim that all three candidates work.
- [ ] second-name registration uses the client's documented CLI syntax and a guarded interpreter; no hand-edited client YAML and no removal or repointing of the existing name.
- [ ] Session credentials live in one root for every process, the refresh lock works across processes, and live account operations run one server at a time.
- [ ] Operations includes swap criteria/rollback by registration, with no rollback of token history or already-applied mutations.
- [ ] B1 is still absent unless its independent gate passes; parity release status is not falsely equated with workout-logging readiness.

### 11.7 Workout logging: evidence and implementation

This is a dedicated extension, not spare work inside the tool surface. First recover the unavailable surface; then implement strict public/wire models, read-back and update snapshots. Follow §6.4's disposable live sequence.

Acceptance:

- [ ] Method/path/body/response support is evidenced for free-tier normal-session create/update, or an explicitly reviewed new client pin provides it; no guessed RawHevyClient routes.
- [ ] Proposed WorkoutInput is reconciled with performed-set/RPE/superset semantics and documented without silent field loss.
- [ ] Create/update have single-attempt failure matrices, read-back identity/content checks and typed unknown/partial outcomes.
- [ ] Replacement snapshot is durable before update; cancellation cannot release the operation lane during a running mutation.
- [ ] Dedicated create→verify→update→verify→guarded-delete→absence record is complete, safe and cleanup status known.
- [ ] Existing delete_workout remains byte/behavior compatible; no new automatic cleanup/fallback in normal tool execution.
- [ ] Both capability gate and opt-in enable flag are enforced; default list remains unchanged until verified. With the verified pair, inventory is 14 or 16 depending on minimal folders.
- [ ] If evidence remains unavailable, finish unaffected releases and leave this work explicitly incomplete; don't advertise working logging or replace it with a stub.

## 12. Risks and decisions that must remain visible

| Risk / evidence gap | Consequence / mitigation |
|---|---|
| Private free-tier endpoints drift | Fail closed on required schemas; retain raw internally, pinned dependency and request-shape fixtures. Never claim Pro API compatibility. |
| “Compact by default” conflicts with v1 raw outputs | Tri-state format/include_raw compatibility contract; old calls unchanged, new modes explicit and described agent-first. |
| Mutable offset paging / bounded windows | Honest continuation, consumed offsets, conservative has_more and scope flags; no global total, snapshot or historical-completeness claim. |
| Catalog described as bundled but absent in wheel | Ship an independently evidenced snapshot; local-only request path, explicit missing-catalog failure and build-data checks. |
| Domain errors masked before middleware / external traceback logs | Safe ToolError bridge plus explicit results and formatter that drops external bodies/tracebacks; test actual protocol failures. |
| Loop-sensitive auth locks and worker cancellation | Async tool/service chain, one lifecycle/operation lane, shield/drain workers; no per-call asyncio.run or assumed worker loop. |
| Minimal folder body/envelope remains Any | Conditional registration after fixtures and own live mutator evidence; no guessed create IDs or title-only recovery. |
| Workout create/update unavailable | Separate, gated extension; no inferred routes, silent dependency upgrade or Pro fallback. |
| Backend candidates not all proven; false certainty after malformed delete | Preserve the exact orchestrator/catch ordering and normal-session verification; retain snapshots and typed unknown/present outcomes. |
| Offline tests can miss real API/child egress behavior | Parent+child socket denial and separate honest live evidence; mocks do not prove endpoint availability or cleanup. |
| Packaging/browser/CLI dependency drift | Exact initial pins and uv lock, 3.12/3.13 wheel checks, browser installation separate, no network during tests. |

The intended implementation changes the server framework, error presentation, documentation, packaging and additive read/upgrade contracts. The proven domain service, authentication, deletion, parsers and client request discipline are carried forward with their original guarantees and limitations. Any later change to those bodies needs its own explicit evidence and review rather than being hidden inside the FastMCP migration.
