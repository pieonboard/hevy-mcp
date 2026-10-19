# Test suite

564 offline tests. They cover protocol behaviour, normalization, cursor arithmetic,
statistics, error mapping, snapshot integrity, resources and the installed wheel on
Python 3.12 and 3.13.

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m pytest
.venv/bin/python scripts/verify_wheel.py
```

## What is covered

| Area | Test files | What they check |
|---|---|---|
| Core domain | `test_service.py`, `test_models.py`, `test_unofficial_adapter.py`, `test_contract_parity.py` | Paging and limit guards, date validation, DTO translation, routine payloads, retry and failure mapping |
| Reads and scanning | `test_scanner.py`, `test_history_batch_tolerance.py` | Consumed offsets across the batch sizes, date windows, scan budgets, duplicate handling, cursor validation and restart, fixed five-row history batches |
| Statistics | `test_analytics.py`, `test_stats_tool.py` | Epley formula, ineligible sets, ties, repeated observations, scope reporting, no extra upstream reads |
| Errors and results | `test_tool_contracts.py`, `test_results_logging.py` | Every error class and hint, all `format`/`include_raw` combinations, hydration, validation, annotations, redaction |
| Tools, resources, transport | `test_server_cli.py`, `test_resources.py`, `test_stdio_protocol.py`, `test_stdio_release.py` | Real stdio subprocess exchange, tool inventory and output schemas, resource discovery and guards, log routing |
| Writes and deletion | `test_partial_writes.py`, `test_service_deletion.py`, `test_delete_client.py`, `test_storage_isolation.py` | Partial and indeterminate write outcomes, snapshot publication, absence verification, storage modes and isolation |
| Catalog | `test_catalog.py`, `test_catalog_import.py` | Checksum and schema validation, local override and fallback behaviour, deterministic search, import paths |
| Runtime and CLI | `test_runtime_lane.py`, `test_cli_scaffold.py`, `test_auth_config_logging.py` | Account lane admission and draining, cancellation, client closure, CLI verbs and exit behaviour, credential handling |
| Client and smoke harness | `test_upgrade_client.py`, `test_smoke_runner.py` | The pinned client behind mocked HTTP, plus the smoke runner's inventory refusal, read-once allowlist, pacing and decline paths |

## How the suite stays offline

- `conftest.py` isolates the home and config roots before collection, so no test reads or
  writes a real account.
- pytest-socket denies TCP in the parent process and allows AF_UNIX only for asyncio
  plumbing. Every child process starts through `child_bootstrap.py` with the same guard
  and a minimal environment with no inherited `PYTHONPATH` or secrets. Both the parent and
  the child denial are exercised deliberately.
- No test installs packages, downloads a browser or reaches Hevy. Browser login and the
  `doctor` browser checks are mocked.
- The stdio tests run a real client session over pipes with fake data and services. A
  post-write failure carries a known routine ID or a synthetic snapshot, never a live one.
- Historical inventory tests select the ten-tool parity profile; CLI discovery expects all
  twelve release tools.

`verify_wheel.py` builds and inspects the wheel and sdist, installs the locked dependency
set and the wheel offline into scratch 3.12 and 3.13 environments, checks site-packages
imports, exercises both CLI entries and reruns the collected suite outside the checkout.
A missing cache or interpreter fails setup instead of allowing a download. Test counts and
module membership come from fresh collection, so the verifier holds no fixed number.

Offline success does not prove that Hevy's private endpoints still accept what this server
sends, nor that routine fields persist, nor that a delete transport works. Those are
operational checks, described in [OPERATIONS.md](../docs/OPERATIONS.md).
