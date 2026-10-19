"""Validated, deterministic local catalog. No SDK fetch or account access here."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

from .boundary import failure
from .config import config_dir
from .results import HevyToolError, StrictView


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


class CatalogRow(StrictView):
    id: str = Field(pattern=r"^[A-F0-9]{8}$")
    title: str = Field(min_length=1)
    muscle_group: str = Field(min_length=1)
    equipment_category: str | None
    exercise_type: str | None
    url: str | None
    thumbnail_url: str | None
    media_type: str | None
    priority: int | None
    is_custom: Literal[False]
    other_muscles: list[str]

    @field_validator("is_custom", mode="before")
    @classmethod
    def builtin_only(cls, value: Any) -> Any:
        if value is not False:
            raise ValueError("catalog must contain built-in boolean false")
        return value

    @field_validator("title", "muscle_group")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank catalog field")
        return value


class CatalogMeta(StrictView):
    format_version: Literal[1]
    version: str = Field(min_length=1)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str = Field(
        pattern=r"^https://hevy\.com/_next/static/chunks/pages/_app-[a-f0-9]+\.js$"
    )
    captured_at: str
    acquired_at: str
    source_count: int = Field(ge=1, strict=True)
    source_document: str = Field(min_length=1)
    source_document_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_bundle_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    parser: Literal["hevy-unofficial==0.0.2a0/ExerciseCatalogStore"]
    acquisition: Literal["store_export", "local_js", "har"]
    attribution: str = Field(min_length=1)

    @field_validator("format_version", mode="before")
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("format version must be integer")
        return value

    @field_validator("captured_at", "acquired_at")
    @classmethod
    def utc_date(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() != timedelta(0):
            raise ValueError("catalog date must be UTC")
        return value


class Template(StrictView):
    exercise_template_id: str
    title: str
    muscle_group: str
    equipment_category: str | None
    exercise_type: str | None
    other_muscles: list[str]
    is_custom: Literal[False] = False
    source: Literal["bundled", "local_cache"]
    units: None = None
    url: str | None
    thumbnail_url: str | None
    media_type: str | None
    priority: int | None


class RawTemplate(Template):
    raw: dict[str, Any]


class CatalogInfo(StrictView):
    version: str
    checksum: str
    source_url: str
    captured_at: str
    loaded_from: Literal["bundled", "local_cache"]
    stale: bool
    source_count: int


class TemplateSearchResult(StrictView):
    items: list[Template | RawTemplate]
    query: str
    muscle_group: str | None
    equipment_category: str | None
    offset: int
    limit: int
    matched_count: int
    returned_count: int
    next_offset: int | None
    has_more: bool
    catalog: CatalogInfo
    custom_included: Literal[False] = False
    custom_cache_age_seconds: None = None
    upstream_complete: None = None
    warnings: list[Literal["invalid_local_catalog_ignored"]] = Field(default_factory=list)


@lru_cache(maxsize=4)
def validated_rows(document: bytes, checksum: str) -> tuple[str, ...]:
    """Cache immutable serialized rows; callers cannot mutate the shared index."""
    if len(document) > 10_000_000 or hashlib.sha256(document).hexdigest() != checksum:
        raise ValueError("catalog checksum mismatch")
    rows = json.loads(document)
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError("invalid catalog array")
    unique: dict[str, str] = {}
    for raw in rows:
        row = CatalogRow.model_validate(raw, strict=True)
        encoded = row.model_dump_json()
        if row.id in unique and unique[row.id] != encoded:
            raise ValueError("conflicting catalog ID")
        unique[row.id] = encoded
    return tuple(unique.values())


def validate_snapshot(document: bytes, metadata: bytes) -> tuple[CatalogMeta, tuple[str, ...]]:
    if len(metadata) > 16384:
        raise ValueError("oversized catalog metadata")
    meta = CatalogMeta.model_validate_json(metadata, strict=True)
    rows = validated_rows(document, meta.checksum)
    if len(rows) != meta.source_count:
        raise ValueError("catalog count mismatch")
    return meta, rows


class Catalog:
    def __init__(self, override_dir: Path | None = None):
        # Resolving config_dir does not create it or read credentials.
        self.override_dir = override_dir if override_dir is not None else config_dir()

    def load(self) -> tuple[CatalogMeta, tuple[str, ...], Literal["bundled", "local_cache"], bool]:
        invalid_override = False
        for root, source in (
            (self.override_dir, "local_cache"),
            (files("hevy_mcp").joinpath("data"), "bundled"),
        ):
            try:
                document = root.joinpath("exercise_catalog.json").read_bytes()
                metadata = root.joinpath("exercise_catalog.meta.json").read_bytes()
                meta, rows = validate_snapshot(document, metadata)
                return (
                    meta,
                    rows,
                    "local_cache" if source == "local_cache" else "bundled",
                    invalid_override,
                )
            except (OSError, ValueError, TypeError):
                if source == "local_cache":
                    invalid_override = (
                        root.joinpath("exercise_catalog.json").is_file()
                        or root.joinpath("exercise_catalog.meta.json").is_file()
                    )
        raise HevyToolError(failure("CATALOG_UNAVAILABLE"))

    def search(
        self,
        query: str = "",
        muscle_group: str | None = None,
        equipment_category: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_custom: bool = False,
        include_raw: bool = False,
    ) -> dict[str, Any]:
        if len(query) > 200 or not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid catalog search bounds")
        query = normalize(query)
        muscle = normalize(muscle_group) if muscle_group is not None else None
        equipment = normalize(equipment_category) if equipment_category is not None else None
        if muscle == "" or equipment == "":
            raise ValueError("blank catalog filter")
        if include_custom:
            raise HevyToolError(failure("CAPABILITY_UNAVAILABLE"))
        meta, encoded_rows, source, invalid = self.load()
        rows = [json.loads(x) for x in encoded_rows]
        matches = [
            row
            for row in rows
            if all(token in normalize(row["title"]) for token in query.split())
            and (muscle is None or normalize(row["muscle_group"]) == muscle)
            and (equipment is None or normalize(row["equipment_category"] or "") == equipment)
        ]
        matches.sort(
            key=lambda row: (normalize(row["title"]) != query, normalize(row["title"]), row["id"])
        )
        items = [
            self.template(row, source, include_raw) for row in matches[offset : offset + limit]
        ]
        next_offset = offset + len(items) if offset + len(items) < len(matches) else None
        captured = datetime.fromisoformat(meta.captured_at.replace("Z", "+00:00"))
        return TemplateSearchResult(
            items=items,
            query=query,
            muscle_group=muscle,
            equipment_category=equipment,
            offset=offset,
            limit=limit,
            matched_count=len(matches),
            returned_count=len(items),
            next_offset=next_offset,
            has_more=next_offset is not None,
            catalog=CatalogInfo(
                version=meta.version,
                checksum=meta.checksum,
                source_url=meta.source_url,
                captured_at=meta.captured_at,
                loaded_from=source,
                stale=datetime.now(UTC) - captured > timedelta(days=7),
                source_count=len(rows),
            ),
            warnings=["invalid_local_catalog_ignored"] if invalid else [],
        ).model_dump(mode="json")

    @staticmethod
    def template(
        row: dict[str, Any], source: Literal["bundled", "local_cache"], raw: bool
    ) -> Template | RawTemplate:
        value = {**row, "exercise_template_id": row["id"], "source": source}
        del value["id"]
        return RawTemplate(**value, raw=row) if raw else Template(**value)

    def get(self, exercise_template_id: str) -> dict[str, Any]:
        _, rows, source, _ = self.load()
        for encoded in rows:
            row = json.loads(encoded)
            if row["id"] == exercise_template_id:
                return self.template(row, source, False).model_dump(mode="json")
        raise HevyToolError(failure("NOT_FOUND"))
