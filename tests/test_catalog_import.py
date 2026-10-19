import importlib.util
import json
from pathlib import Path

import pytest

from hevy_mcp.catalog import validate_snapshot

spec = importlib.util.spec_from_file_location(
    "hevy_catalog_import", Path(__file__).resolve().parents[1] / "scripts/import_catalog.py"
)
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


def test_explicit_local_js_pipeline_uses_pinned_parser_and_independent_validation(
    tmp_path, monkeypatch
):
    from hevy_unofficial.exercise_catalog import ExerciseCatalogStore

    monkeypatch.setattr(
        ExerciseCatalogStore,
        "fetch_and_update",
        lambda *args, **kw: pytest.fail("network forbidden"),
    )
    source = tmp_path / "_app-123abc.js"
    # Synthetic parser fixture stays only in tmp_path, never in production data.
    source.write_text(
        'const a=[{"id":"ABCDEF12","title":"Test Squat","muscle_group":"quadriceps","is_custom":false}];'
    )
    data, meta = maintenance.acquire(
        source,
        "local_js",
        "https://hevy.com/_next/static/chunks/pages/_app-123abc.js",
        "2026-01-01T00:00:00Z",
    )
    provenance, rows = validate_snapshot(data, meta)
    assert len(rows) == 1 and provenance.acquisition == "local_js"
    assert provenance.source_bundle_checksum == provenance.source_document_checksum
    assert provenance.source_document == source.name


def test_empty_parser_and_mismatched_export_are_rejected(tmp_path):
    source = tmp_path / "local.js"
    source.write_text("no catalog present")
    with pytest.raises(ValueError):
        maintenance.acquire(
            source,
            "local_js",
            "https://hevy.com/_next/static/chunks/pages/_app-123abc.js",
            "2026-01-01T00:00:00Z",
        )
    source.write_text(
        json.dumps(
            {
                "meta": {
                    "source_url": "https://example.test/",
                    "fetched_at": "2026-01-01T00:00:00Z",
                    "parsed_at": "2026-01-01T00:00:00Z",
                    "exercise_count": 0,
                },
                "exercises": [],
            }
        )
    )
    with pytest.raises(ValueError, match="provenance"):
        maintenance.acquire(
            source,
            "store_export",
            "https://hevy.com/_next/static/chunks/pages/_app-123abc.js",
            "2026-01-01T00:00:00Z",
        )


def test_har_pipeline_checks_actual_origin_and_never_ships_capture(tmp_path):
    url = "https://hevy.com/_next/static/chunks/pages/_app-123abc.js"
    js = '[{"id":"ABCDEF12","title":"Test Squat","muscle_group":"quadriceps","is_custom":false}]'
    source = tmp_path / "capture.har"
    source.write_text(
        json.dumps(
            {"log": {"entries": [{"request": {"url": url}, "response": {"content": {"text": js}}}]}}
        )
    )
    data, meta = maintenance.acquire(source, "har", url, "2026-01-01T00:00:00Z")
    provenance, rows = validate_snapshot(data, meta)
    assert provenance.acquisition == "har" and len(rows) == 1
    assert provenance.source_bundle_checksum is None
    assert b"log" not in data
    with pytest.raises(ValueError, match="provenance"):
        maintenance.acquire(source, "har", url.replace("123abc", "ffff"), "2026-01-01T00:00:00Z")
