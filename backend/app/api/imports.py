"""Data-source listing and import endpoints.

GeoJSON, CSV and plan-JSON uploads are registered only: they do not parse into
cadastral records and do not alter the active scene.

**Point clouds (LAS/LAZ/PLY) are genuinely inspected** by the ingestion pipeline
in :mod:`app.services.point_cloud_ingestion`, which streams the upload to disk,
hashes it, reads its header, resolves its CRS and extent, then records a source
and processing jobs. Their points are never loaded.
"""
from __future__ import annotations

from typing import Any

import anyio
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import as_http_error, request_repository
from app.repositories.base import CadastreRepository
from app.services import ingestion, point_cloud, point_cloud_ingestion

router = APIRouter(tags=["ingestion"])


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
    raw = await file.read()
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

    raw = await file.read()
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
