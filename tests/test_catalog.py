"""Real bundled catalog and corruption/override tests; no remote acquisition."""

import hashlib
import json
from importlib.resources import files

import jsonschema
import pytest
from fakes import FakeService
from fastmcp import Client

from hevy_mcp import catalog
from hevy_mcp.catalog import Catalog, validate_snapshot
from hevy_mcp.results import HevyToolError
from hevy_mcp.server import create_server


def snapshot():
    root = files("hevy_mcp").joinpath("data")
    return root.joinpath("exercise_catalog.json").read_bytes(), root.joinpath(
        "exercise_catalog.meta.json"
    ).read_bytes()


def write_snapshot(root, rows=None, meta_changes=None):
    document, metadata = snapshot()
    meta = json.loads(metadata)
    if rows is not None:
        document = json.dumps(rows).encode()
        meta.update(
            checksum=hashlib.sha256(document).hexdigest(),
            source_count=len({x.get("id") for x in rows}),
        )
    meta.update(meta_changes or {})
    root.mkdir(parents=True, exist_ok=True)
    (root / "exercise_catalog.json").write_bytes(document)
    (root / "exercise_catalog.meta.json").write_text(json.dumps(meta))


def test_evidenced_snapshot_checksums_and_complete_strict_rows():
    document, metadata = snapshot()
    meta, rows = validate_snapshot(document, metadata)
    assert len(rows) == meta.source_count == 451
    assert (
        meta.source_document_checksum
        == "4edbe47ef6b3caa223c53059353c694d0fe01ecdd7ba1e4d509b9002362b72d8"
    )
    assert meta.source_bundle_checksum is None  # No fabricated original JS hash.
    assert meta.acquisition == "store_export"
    assert meta.source_url.endswith("_app-5fa98d71810f0958.js")
    assert {json.loads(x)["id"] for x in rows} == {x["id"] for x in json.loads(document)}


async def test_release_search_and_resources_offline_without_credentials(tmp_path, monkeypatch):
    from hevy_unofficial.exercise_catalog import ExerciseCatalogStore

    def forbidden(*args, **kwargs):
        raise AssertionError("Catalog network/SDK cache fetch forbidden")

    monkeypatch.setattr(ExerciseCatalogStore, "fetch_and_update", forbidden)
    fake = FakeService()
    fake.adapter.failure = AssertionError("Account IO forbidden")
    server = create_server(lambda: fake)
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        result = await client.call_tool(
            "search_exercise_templates",
            {"query": "  BENCH   press ", "equipment_category": " BARBELL "},
        )
        data = result.structured_content
        assert data["items"] and data["query"] == "bench press"
        assert all(x["equipment_category"] == "barbell" for x in data["items"])
        assert not any("raw" in x for x in data["items"])
        jsonschema.validate(data, tools["search_exercise_templates"].output_schema)
        resource = await client.read_resource(
            "hevy://templates/" + data["items"][0]["exercise_template_id"]
        )
        assert json.loads(resource[0].text) == data["items"][0]
    assert not (tmp_path / "hevy").exists()
    assert not fake.batches and not fake.adapter.calls
    # Actual default factory also works without a session, not just a fake service.
    result = await create_server().call_tool("search_exercise_templates", {"limit": 1})
    assert not result.is_error and result.structured_content["returned_count"] == 1


def test_deterministic_tokens_exact_filters_order_and_offsets():
    store = Catalog()
    all_rows = []
    for offset in range(0, 451, 100):
        result = store.search(limit=100, offset=offset)
        all_rows.extend(result["items"])
        assert result["matched_count"] == 451
    assert len({x["exercise_template_id"] for x in all_rows}) == 451
    assert all_rows == sorted(
        all_rows, key=lambda x: (catalog.normalize(x["title"]), x["exercise_template_id"])
    )
    query = all_rows[40]["title"]
    exact = store.search(query=query)
    assert exact["items"][0]["title"] == query
    filtered = store.search(
        query="press bench", muscle_group=" CHEST ", equipment_category="BarBell", limit=1
    )
    assert filtered["items"]
    item = filtered["items"][0]
    assert all(token in item["title"].casefold() for token in ("press", "bench"))
    assert item["muscle_group"] == "chest"
    assert filtered["next_offset"] == 1 and filtered["has_more"]
    assert store.search(muscle_group="does-not-exist")["items"] == []
    assert store.search(query="press", offset=9999)["next_offset"] is None
    assert store.search(query="пресс")["items"] == []  # No transliteration/fuzzy IDs.


def test_index_is_immutable_to_returned_raw_mutation():
    first = Catalog().search(limit=1, include_raw=True)
    first["items"][0]["raw"]["other_muscles"].append("injected")
    first["items"][0]["title"] = "injected"
    second = Catalog().search(limit=1, include_raw=True)
    assert "injected" not in str(second)
    assert second["catalog"]["stale"] is True
    assert second["items"][0]["units"] is None


async def test_custom_unavailable_before_catalog_or_account_io(monkeypatch):
    monkeypatch.setattr(Catalog, "load", lambda self: pytest.fail("catalog should not load"))
    result = await create_server(lambda: None).call_tool(
        "search_exercise_templates", {"include_custom": True}
    )
    assert (
        result.is_error and result.structured_content["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    )


@pytest.mark.parametrize(
    "args",
    [
        {"query": "x" * 201},
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"muscle_group": " "},
        {"equipment_category": ""},
    ],
)
async def test_search_invalid_input_typed_before_io(args):
    result = await create_server(lambda: None).call_tool("search_exercise_templates", args)
    assert result.is_error and result.structured_content["error"]["code"] == "INVALID_ARGUMENT"


def test_override_precedence_duplicate_dedup_and_corrupt_fallback(tmp_path):
    rows = json.loads(snapshot()[0])[:1]
    write_snapshot(tmp_path, rows=rows)
    assert Catalog(tmp_path).search()["catalog"]["loaded_from"] == "local_cache"
    # Identical duplicate IDs do not inflate the public count.
    document = json.dumps(rows * 2).encode()
    meta = json.loads((tmp_path / "exercise_catalog.meta.json").read_text())
    meta["checksum"] = hashlib.sha256(document).hexdigest()
    (tmp_path / "exercise_catalog.json").write_bytes(document)
    (tmp_path / "exercise_catalog.meta.json").write_text(json.dumps(meta))
    assert Catalog(tmp_path).search()["matched_count"] == 1
    (tmp_path / "exercise_catalog.json").write_bytes(b"[]")
    fallback = Catalog(tmp_path).search()
    assert fallback["catalog"]["loaded_from"] == "bundled"
    assert fallback["warnings"] == ["invalid_local_catalog_ignored"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("id", 123),
        ("id", "not-a-parser-id"),
        ("title", " "),
        ("muscle_group", None),
        ("priority", True),
        ("equipment_category", []),
        ("is_custom", True),
        ("is_custom", 0),
        ("other_muscles", [123]),
        ("unknown", 1),
    ],
)
def test_own_loader_rejects_invalid_rows_with_matching_checksum(key, value):
    document, metadata = snapshot()
    row = json.loads(document)[0]
    row[key] = value
    data = json.dumps([row]).encode()
    meta = json.loads(metadata)
    meta.update(checksum=hashlib.sha256(data).hexdigest(), source_count=1)
    with pytest.raises(ValueError):
        validate_snapshot(data, json.dumps(meta).encode())


@pytest.mark.parametrize(
    "changes",
    [
        {"format_version": 2},
        {"format_version": True},
        {"format_version": 1.0},
        {"source_count": 999},
        {"checksum": "0" * 64},
        {"source_url": "https://example.test/fake.js"},
        {"captured_at": "2026-01-01"},
        {"source_document_checksum": None},
        {"parser": "unreviewed"},
    ],
)
def test_provenance_and_checksum_fail_closed(changes):
    document, metadata = snapshot()
    meta = json.loads(metadata)
    meta.update(changes)
    with pytest.raises(ValueError):
        validate_snapshot(document, json.dumps(meta).encode())


def test_missing_required_row_fields_and_conflicting_ids():
    document, metadata = snapshot()
    rows = json.loads(document)[:1]
    meta = json.loads(metadata)
    for variant in (
        {k: v for k, v in rows[0].items() if k != "title"},
        {**rows[0], "title": "Conflicting"},
    ):
        data = json.dumps([rows[0], variant]).encode()
        meta.update(checksum=hashlib.sha256(data).hexdigest(), source_count=1)
        with pytest.raises(ValueError):
            validate_snapshot(data, json.dumps(meta).encode())


async def test_missing_snapshot_tool_and_resources_remain_typed(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "files", lambda name: tmp_path)
    with pytest.raises(HevyToolError):
        Catalog(tmp_path).search()
    server = create_server(lambda: None)
    result = await server.call_tool("search_exercise_templates", {})
    assert result.is_error and result.structured_content["error"]["code"] == "CATALOG_UNAVAILABLE"
    for uri in ("hevy://templates", "hevy://templates/3BC06AD3"):
        resource = await server.read_resource(uri)
        assert json.loads(resource.contents[0].content)["error"]["code"] == "CATALOG_UNAVAILABLE"
        assert resource.meta["hevy"]["status"] == "error"
    help_result = await server.read_resource("hevy://help")
    assert json.loads(help_result.contents[0].content)["catalog_status"] == "unavailable"
