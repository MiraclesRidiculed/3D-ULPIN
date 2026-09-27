"""Import handling for cadastral source data.

**Current behaviour is registration only.** Both entry points record a metadata
row describing the upload; neither parses the payload into cadastral records
and neither mutates the active scene. In particular:

* GeoJSON is parsed solely to count ``features``.
* CSV and floor-plan JSON are not parsed at all.
* LAS / LAZ / PLY are *not processed*. The extension is mapped to a label and
  the bytes are measured, never interpreted. No point-cloud library is present.

This is a deliberate placeholder boundary; see ``PROJECT_CONTEXT.md``.
"""
from __future__ import annotations

import json
from typing import Any

from app.config import MAX_UPLOAD_BYTES
from app.models.enums import AuditAction, SourceType
from app.repositories.base import CadastreRepository
from app.services.audit import create_audit_event
from app.services.crs import UNKNOWN_CRS, declared_crs_from_geojson
from app.utils import now

#: Extensions accepted by ``/import/geojson``.
GEOJSON_EXTENSIONS: tuple[str, ...] = (".geojson", ".json")

#: Extension -> human label, accepted by ``/import/source``.
SUPPORTED_EXTENSIONS: dict[str, str] = {
    "geojson": SourceType.GEOJSON.value,
    "json": SourceType.FLOOR_PLAN_JSON.value,
    "csv": SourceType.CSV.value,
    "las": SourceType.POINT_CLOUD.value,
    "laz": SourceType.POINT_CLOUD.value,
    "ply": SourceType.POINT_CLOUD.value,
}

SOURCE_TYPE_AUTO = "auto"
MAX_FILENAME_LENGTH = 255
MAX_SOURCE_TYPE_LENGTH = 50


class IngestionError(Exception):
    """Domain error carrying the status code the API should surface."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _next_source_id(repo: CadastreRepository) -> str:
    """Sequential source id, continuing past whatever is already stored."""
    existing = repo.records("sources")
    highest = 0
    for record in existing:
        raw = str(record.get("id", ""))
        if raw.startswith("DS-") and raw[3:].isdigit():
            highest = max(highest, int(raw[3:]))
    return f"DS-{highest + 1:03d}" if existing else "DS-001"


def _register(
    repo: CadastreRepository,
    *,
    source_type: str,
    filename: str,
    crs: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    timestamp = now()
    record = {
        "id": _next_source_id(repo),
        "source_type": source_type,
        "filename": filename,
        "crs": crs,
        "acquisition_date": timestamp[:10],
        "metadata": metadata,
        # Explicit so the in-memory store matches PostGIS, where the column
        # defaults server-side.
        "created_at": timestamp,
    }
    repo.add("sources", record)
    create_audit_event(
        repo,
        action=AuditAction.IMPORTED.value,
        object_id=record["id"],
        object_type="DATA_SOURCE",
        detail=f"Registered {source_type} source {record['id']} ({filename})",
        extra={
            "filename": record["filename"],
            "source_type": source_type,
            "crs": crs,
            "bytes": metadata.get("bytes"),
        },
    )
    return record


def register_geojson(
    repo: CadastreRepository, filename: str, raw: bytes
) -> dict[str, Any]:
    """Register a GeoJSON upload. Counts features; does not create records.

    The CRS is read from the document rather than assumed, and preserved on the
    source record so downstream processing can reproject correctly. A payload
    that declares nothing gets WGS84 (fixed by RFC 7946); a payload with an
    unusable ``crs`` member is recorded as :data:`~app.services.crs.UNKNOWN_CRS`
    rather than guessed at.
    """
    if not filename or not filename.lower().endswith(GEOJSON_EXTENSIONS):
        raise IngestionError(400, "Upload GeoJSON or JSON")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise IngestionError(413, "Maximum upload size is 10 MB")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise IngestionError(400, "Invalid JSON") from None

    # NOTE: a valid-JSON non-object body (e.g. a bare array) raises
    # AttributeError here, yielding a 500. Preserved deliberately from the
    # original implementation rather than silently altered during the refactor.
    features = len(data.get("features", []))
    declared = declared_crs_from_geojson(data)
    _register(
        repo,
        source_type=SourceType.GEOJSON.value,
        filename=filename,
        crs=declared,
        metadata={
            "features": features,
            "source_crs": declared,
            "crs_declared_in_payload": "crs" in data,
        },
    )
    return {
        "accepted": True,
        "features": features,
        "source_crs": declared,
        "note": (
            "Source registered. Production adapter would persist imported "
            "features to PostGIS."
        ),
    }


def register_source(
    repo: CadastreRepository,
    filename: str,
    raw: bytes,
    source_type: str = SOURCE_TYPE_AUTO,
    *,
    crs: str | None = None,
) -> dict[str, Any]:
    """Register a bounded demo input (GeoJSON, CSV, plan JSON, point cloud).

    The payload is never interpreted; only its length is recorded.

    ``crs`` may be supplied by the caller when the format carries no CRS of its
    own (CSV, LAS/LAZ/PLY). It is preserved on the source record. When omitted it
    is recorded as :data:`~app.services.crs.UNKNOWN_CRS` — never defaulted to
    WGS84, because assuming a CRS silently mis-places data.
    """
    if not filename:
        raise IngestionError(400, "A filename is required")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        raise IngestionError(
            400, "Supported inputs: GeoJSON, JSON, CSV, LAS/LAZ, or PLY"
        )
    if len(raw) > MAX_UPLOAD_BYTES:
        raise IngestionError(413, "Maximum upload size is 10 MB")

    detected = (
        SUPPORTED_EXTENSIONS[ext]
        if source_type == SOURCE_TYPE_AUTO
        else source_type[:MAX_SOURCE_TYPE_LENGTH]
    )
    declared = crs if crs else UNKNOWN_CRS
    _register(
        repo,
        source_type=detected,
        filename=filename[:MAX_FILENAME_LENGTH],
        crs=declared,
        metadata={
            "bytes": len(raw),
            "ingestion": "registered for normalization",
            "source_crs": declared,
            "crs_supplied_by_caller": bool(crs),
        },
    )
    return {
        "accepted": True,
        "source_type": detected,
        "filename": filename,
        "bytes": len(raw),
        "source_crs": declared,
        "note": (
            "Source accepted into the normalization queue. The demo city "
            "remains the active processing scene."
        ),
    }
