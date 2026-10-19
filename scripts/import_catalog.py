"""Explicit offline maintenance: import a supplied JS, HAR or CatalogStore export.

Never called by build/startup/search/tests. Raw JS/HAR is never redistributed.
The source URL and capture time document origin; they are never fetched.
"""

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from hevy_unofficial.exercise_catalog import ExerciseCatalogStore

from hevy_mcp.catalog import validate_snapshot


def acquire(source: Path, kind: str, source_url: str, captured_at: str) -> tuple[bytes, bytes]:
    if version("hevy-unofficial") != "0.0.2a0":
        raise ValueError("Catalog parser pin changed; review before import")
    original = source.read_bytes()
    with tempfile.TemporaryDirectory(prefix="hevy-catalog-import-") as directory:
        store = ExerciseCatalogStore(Path(directory))
        if kind == "local_js":
            rows = store.parse_local_js_file(source)
        elif kind == "har":
            rows = store.parse_app_js_from_har(source)
            upstream_meta, _ = store.load()
            if upstream_meta is None or upstream_meta.source_url != source_url:
                raise ValueError("HAR provenance differs from supplied origin")
        elif kind == "store_export":
            store.catalog_path.write_bytes(original)
            upstream_meta, rows = store.load()
            if (
                upstream_meta is None
                or upstream_meta.source_url != source_url
                or upstream_meta.fetched_at != captured_at
                or upstream_meta.exercise_count != len(rows)
            ):
                raise ValueError("Export provenance differs from supplied origin")
        else:
            raise ValueError("Unsupported offline input")
        document = (json.dumps(rows, ensure_ascii=False, indent=2) + "\n").encode()
        metadata = (
            json.dumps(
                {
                    "format_version": 1,
                    "version": "hevy-catalog-" + captured_at[:10],
                    "checksum": hashlib.sha256(document).hexdigest(),
                    "source_url": source_url,
                    "captured_at": captured_at,
                    "acquired_at": datetime.now(UTC).isoformat(),
                    "source_count": len({row["id"] for row in rows}),
                    "source_document": source.name,
                    "source_document_checksum": hashlib.sha256(original).hexdigest(),
                    "source_bundle_checksum": hashlib.sha256(original).hexdigest()
                    if kind == "local_js"
                    else None,
                    "parser": "hevy-unofficial==0.0.2a0/ExerciseCatalogStore",
                    "acquisition": kind,
                    "attribution": "Hevy exercise definitions and media references; see ATTRIBUTION.md. No media files bundled.",
                },
                indent=2,
            )
            + "\n"
        ).encode()
        validate_snapshot(document, metadata)  # Independent schema/checksum gate before output.
        return document, metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--kind", choices=("local_js", "har", "store_export"), required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--captured-at", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    document, metadata = acquire(args.source, args.kind, args.source_url, args.captured_at)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "exercise_catalog.json").write_bytes(document)
    (args.output_dir / "exercise_catalog.meta.json").write_bytes(metadata)
    print("Validated offline catalog imported; no remote request made.")


if __name__ == "__main__":
    main()
