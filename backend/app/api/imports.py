"""Data-source listing and import endpoints.

GeoJSON, CSV and plan-JSON uploads are registered only: they do not parse into
cadastral records and do not alter the active scene.

**Point clouds (LAS/LAZ/PLY) are genuinely inspected** by the ingestion pipeline
in :mod:`app.services.point_cloud_ingestion`, which streams the upload to disk,
hashes it, reads its header, resolves its CRS and extent, then records a source
and processing jobs. Their points are never loaded.

Every upload path is size-bounded *while it is being read*
(:func:`read_bounded`), never after. ``await file.read()`` materialises the whole
request body as one ``bytes`` object, so a size check that runs afterwards does
not bound anything: the allocation has already happened. Reading in chunks and
aborting at the cap means an oversized upload is refused while it is still
arriving, and no more than the cap is ever resident.
"""
from __future__ import annotations

from typing import Any

import anyio
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import as_http_error, request_repository
from app.config import MAX_UPLOAD_BYTES
from app.repositories.base import CadastreRepository
from app.services import ingestion, point_cloud, point_cloud_ingestion

router = APIRouter(tags=["ingestion"])

#: Read granularity for the bounded upload reader. Reuses the point-cloud
#: pipeline's own chunk size so there is one constant for "how much of a body do
#: we hold at once" rather than two.
UPLOAD_CHUNK_BYTES = point_cloud.HASH_CHUNK_BYTES


async def read_bounded(
    file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES
) -> bytes:
    """Read an upload into memory, refusing to exceed ``max_bytes``.

    Reads ``file.file`` incrementally rather than using ``file.read()``, so the
    body is never materialised whole. A ``Content-Length`` is treated as a hint
    only -- it is client-supplied and a client can understate it -- so the limit
    is enforced against the bytes actually received. The result is at most
    ``max_bytes`` long, so peak memory is bounded by the cap rather than by
    whatever the caller chose to send.

    The point-cloud route does not use this: it spools to disk instead, because
    point clouds are large and must be retained anyway.
    """
    stream = file.file
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = await anyio.to_thread.run_sync(stream.read, UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, "Maximum upload size is 10 MB")
        parts.append(chunk)
    return b"".join(parts)


@router.get("/data-sources")
def list_data_sources(
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    return repository.records("sources")


@router.post("/import/geojson")
async def import_geojson(
    file: UploadFile = File(...),
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """Register a GeoJSON upload. Counts features; creates no records."""
    raw = await read_bounded(file)
    try:
        return ingestion.register_geojson(repository, file.filename or "", raw)
    except ingestion.IngestionError as exc:
        raise as_http_error(exc) from None


@router.post("/import/source")
async def import_source(
    file: UploadFile = File(...),
    source_type: str = ingestion.SOURCE_TYPE_AUTO,
    crs: str | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """Register an input: GeoJSON, CSV, plan JSON, or a point cloud.

    Point clouds are routed to the real ingestion pipeline and inspected; other
    formats are registered only, as before. ``crs`` may be supplied for formats
    that carry no CRS of their own and is preserved rather than assumed.
    """
    filename = file.filename or ""
    if point_cloud.is_point_cloud_filename(filename):
        return await _ingest_point_cloud(file, filename, crs, repository)

    raw = await read_bounded(file)
    try:
        return ingestion.register_source(
            repository, filename, raw, source_type, crs=crs
        )
    except ingestion.IngestionError as exc:
        raise as_http_error(exc) from None


async def _ingest_point_cloud(
    file: UploadFile,
    filename: str,
    crs: str | None,
    repository: CadastreRepository,
) -> dict[str, Any]:
    """Run the point-cloud pipeline against the request's upload stream.

    ``file.file`` is the underlying binary stream, so the upload is read in
    chunks straight to disk: never buffered whole in memory, and the size cap is
    enforced while streaming. The work runs in a worker thread because it is
    blocking file I/O.
    """

    def run() -> dict[str, Any]:
        return point_cloud_ingestion.ingest_point_cloud(
            repository, file.file, filename, crs_override=crs
        )

    try:
        return await anyio.to_thread.run_sync(run)
    except point_cloud_ingestion.IngestionFailure as exc:
        raise HTTPException(exc.status_code, exc.detail) from None
