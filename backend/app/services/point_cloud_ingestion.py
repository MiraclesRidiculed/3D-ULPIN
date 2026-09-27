"""Point-cloud ingestion pipeline.

Implements the agreed order, with each step independently observable:

1. upload received
2. validate extension
3. validate size
4. calculate file hash
5. read metadata (header only)
6. determine CRS
7. determine bounds
8. determine point count
9. register source
10. create processing job and store results

Two properties matter more than the sequence:

**Nothing is buffered whole.** The upload is streamed to a temporary file in
chunks while being hashed, and the size cap is enforced *during* the stream, so
an oversized upload is abandoned early instead of after sitting in memory.

**CRS is never guessed.** A file that declares none is stored as
:data:`~app.services.crs.UNKNOWN_CRS` with a recorded reason, because defaulting
to WGS84 silently mis-places data.
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from app.config import MAX_UPLOAD_BYTES
from app.models.enums import AuditAction, JobStatus, ProcessingJobType, SourceType
from app.models.schemas import ProcessingJob
from app.repositories.base import CadastreRepository
from app.services import point_cloud as pc_service
from app.services import point_cloud_store as pc_store
from app.services.audit import create_audit_event
from app.services.crs import UNKNOWN_CRS, WGS84
from app.utils import now

#: Job types recorded for every ingested point cloud. Every stage below is
#: implemented and runs on request, so each is recorded as ``PENDING`` rather
#: than ``NOT_IMPLEMENTED``: nothing here is silently missing.
PIPELINE_STAGES: tuple[tuple[ProcessingJobType, str], ...] = (
    (
        ProcessingJobType.METADATA_EXTRACTION,
        "Header, CRS, extent and point count read from the file",
    ),
    (
        ProcessingJobType.BUILDING_EXTRACTION,
        "Algorithmic building extraction available on request via "
        "POST /point-clouds/{source_id}/extract",
    ),
    (
        ProcessingJobType.FLOOR_SEGMENTATION,
        "Algorithmic storey segmentation available on request via "
        "POST /point-clouds/{source_id}/segment-floors",
    ),
)


class IngestionFailure(Exception):
    """Ingestion could not complete. Carries an HTTP-facing status code."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = message


def _serialise(bounds: Any, crs: str) -> dict[str, Any]:
    return {
        "min_x": bounds.min_x,
        "min_y": bounds.min_y,
        "min_z": bounds.min_z,
        "max_x": bounds.max_x,
        "max_y": bounds.max_y,
        "max_z": bounds.max_z,
        "crs": crs,
    }


def _display_bounds_dict(metadata: pc_service.PointCloudMetadata) -> dict[str, Any] | None:
    """WGS84 bounds, or ``None`` when the file's CRS is unknown.

    Never falls back to source-CRS numbers under a display heading: a consumer
    reading these must be able to assume degrees.
    """
    if metadata.display_bounds is None:
        return None
    return _serialise(metadata.display_bounds, metadata.display_crs)


def _source_bounds_dict(metadata: pc_service.PointCloudMetadata) -> dict[str, Any] | None:
    """Bounds in the file's own CRS, or ``None`` when it declares no extent."""
    if not metadata.bounds.is_defined:
        return None
    return _serialise(metadata.bounds, metadata.crs)


def _next_source_id(repo: CadastreRepository) -> str:
    """Sequential source id continuing past whatever is stored."""
    highest = 0
    for record in repo.records("sources"):
        raw = str(record.get("id", ""))
        if raw.startswith("DS-") and raw[3:].isdigit():
            highest = max(highest, int(raw[3:]))
    return f"DS-{highest + 1:03d}" if repo.records("sources") else "DS-001"


def ingest_point_cloud(
    repo: CadastreRepository,
    stream: BinaryIO,
    filename: str,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
    crs_override: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Run the full pipeline for one uploaded point cloud.

    Parameters
    ----------
    stream:
        A binary file-like object positioned at the start. Read in chunks, never
        slurped.
    crs_override:
        Supplied when the file declares no CRS but the caller knows it (some
        survey exports state it out of band). Recorded as the source of truth.
    """
    # Step 2: extension.
    try:
        fmt = pc_service.format_for_filename(filename)
    except pc_service.PointCloudError as exc:
        raise IngestionFailure(exc.detail, exc.status_code) from None

    # Steps 3 and 4: size limit enforced during the stream, hashing as we go.
    handle_path = Path(tempfile.mkdtemp(prefix="vcad-pc-")) / f"upload.{fmt.value.lower()}"
    try:
        try:
            size_bytes, digest = pc_service.spool_upload(stream, handle_path, max_bytes)
        except pc_service.PointCloudError as exc:
            raise IngestionFailure(exc.detail, exc.status_code) from None

        # Step 5: header-only metadata.
        try:
            handle = pc_service.read_point_cloud(
                handle_path, filename, sha256=digest, file_size_bytes=size_bytes
            )
        except pc_service.PointCloudError as exc:
            raise IngestionFailure(exc.detail, exc.status_code) from None

        # Step 6: CRS. A caller-supplied CRS wins over an undeclared one; it
        # never overrides what the file actually states.
        declared_crs = pc_service.detect_point_cloud_crs(handle)
        effective_crs = declared_crs
        crs_origin = handle.crs_source
        if declared_crs == UNKNOWN_CRS and crs_override:
            try:
                from app.services.crs import crs_authority

                effective_crs = crs_authority(crs_override)
                crs_origin = "caller_supplied"
            except Exception:  # noqa: BLE001
                raise IngestionFailure(
                    f"Supplied CRS {crs_override!r} could not be resolved", 400
                ) from None

        validation = pc_service.validate_point_cloud(handle)
        metadata = pc_service.get_point_cloud_metadata(handle)
        metadata.crs = effective_crs
        metadata.crs_source = crs_origin
        if effective_crs != declared_crs:
            # A caller-supplied CRS makes the file interpretable, so the display
            # bounds must be recomputed: they were skipped as unknown above.
            metadata.display_bounds = pc_service.display_bounds_for(
                metadata.bounds, effective_crs
            )
            metadata.processing_crs = pc_service.processing_crs_for_bounds(
                metadata.bounds, effective_crs
            )
            metadata.density_points_per_m2 = pc_service.point_density_for_bounds(
                metadata.bounds, effective_crs, metadata.point_count
            )

        # Steps 7 and 8 are already resolved by the engine; surface them.
        bounds = pc_service.calculate_point_cloud_bounds(handle)
        point_count = pc_service.calculate_point_count(handle)
        density = metadata.density_points_per_m2

        # Step 9: register the source with full provenance.
        source_id = _next_source_id(repo)
        source_metadata: dict[str, Any] = {
            "point_cloud": True,
            "format": metadata.format,
            "compressed": metadata.compressed,
            "sha256": digest,
            "file_size_bytes": size_bytes,
            "point_count": point_count,
            "source_crs": effective_crs,
            "processing_crs": metadata.processing_crs,
            "display_crs": metadata.display_crs,
            "crs_source": crs_origin,
            "crs_declared_in_payload": declared_crs != UNKNOWN_CRS,
            "density_points_per_m2": density,
            "point_format": metadata.point_format,
            "file_version": metadata.file_version,
            "extra_dimensions": metadata.extra_dimensions,
            "properties": metadata.properties,
            "comments": metadata.comments,
            "acquisition": metadata.acquisition,
            "source_bounds": _source_bounds_dict(metadata),
            "display_bounds": _display_bounds_dict(metadata),
            "validation": {
                "is_valid": validation.is_valid,
                "problems": validation.problems,
                "warnings": validation.warnings,
            },
            "ingestion": "point cloud inspected (header only)",
        }

        # Retain the upload. Metadata inspection only needed the header, but
        # building extraction has to re-read the points, so the file has to
        # outlive its own request.
        retained_at: str | None = None
        try:
            stored = pc_store.store_upload(source_id, filename, handle_path)
            retained_at = pc_store.relative_path(stored)
            source_metadata["point_cloud_storage_path"] = retained_at
        except OSError as exc:
            # Ingestion still succeeded; extraction simply will not be possible.
            source_metadata["point_cloud_storage_path"] = None
            source_metadata["point_cloud_storage_error"] = str(exc)

        repo.add(
            "sources",
            {
                "id": source_id,
                "source_type": SourceType.POINT_CLOUD.value,
                "filename": filename,
                "crs": effective_crs,
                "source_crs": effective_crs,
                "processing_crs": metadata.processing_crs,
                "display_crs": metadata.display_crs,
                "acquisition_date": now()[:10],
                "metadata": source_metadata,
                "created_at": now(),
            },
        )
        create_audit_event(
            repo,
            action=AuditAction.IMPORTED.value,
            object_id=source_id,
            object_type="DATA_SOURCE",
            actor=created_by,
            detail=(
                f"Registered point cloud {source_id} ({filename}, "
                f"{point_count} points, {metadata.format})"
            ),
            extra={
                "filename": filename,
                "format": metadata.format,
                "crs": effective_crs,
                "point_count": point_count,
                "sha256": digest,
                "retained_path": retained_at,
            },
        )

        # Step 10: processing jobs. Metadata extraction succeeded; the
        # extraction stages are recorded as not implemented.
        jobs: list[dict[str, Any]] = []
        started = now()
        jobs.append(
            repo.add(
                "processing_jobs",
                {
                    "id": f"JOB-{uuid.uuid4().hex[:12]}",
                    "source_id": source_id,
                    "job_type": ProcessingJobType.METADATA_EXTRACTION.value,
                    "status": (
                        JobStatus.COMPLETED.value
                        if validation.is_valid
                        else JobStatus.FAILED.value
                    ),
                    "point_count": point_count,
                    "crs": effective_crs,
                    "bounds": _display_bounds_dict(metadata),
                    "detail": "Header, CRS, extent and point count read from the file",
                    "error": "; ".join(validation.problems) or None,
                    "started_at": started,
                    "completed_at": now(),
                    "created_by": created_by,
                    "metadata": {
                        "format": metadata.format,
                        "density_points_per_m2": density,
                        "warnings": validation.warnings,
                        "sha256": digest,
                    },
                },
            )
        )
        for job_type, detail in PIPELINE_STAGES[1:]:
            # Both downstream stages are implemented but have not been asked for,
            # so they are PENDING: available, not missing.
            jobs.append(
                repo.add(
                    "processing_jobs",
                    {
                        "id": f"JOB-{uuid.uuid4().hex[:12]}",
                        "source_id": source_id,
                        "job_type": job_type.value,
                        "status": JobStatus.PENDING.value,
                        "point_count": point_count,
                        "crs": effective_crs,
                        "bounds": _display_bounds_dict(metadata),
                        "detail": detail,
                        "started_at": started,
                        "completed_at": now(),
                        "created_by": created_by,
                        "metadata": {},
                    },
                )
            )

        return {
            "accepted": True,
            "source_id": source_id,
            "format": metadata.format,
            "point_count": point_count,
            "crs": effective_crs,
            "source_crs": effective_crs,
            "processing_crs": metadata.processing_crs,
            "display_crs": metadata.display_crs,
            "crs_source": crs_origin,
            "bounds": _source_bounds_dict(metadata),
            "display_bounds": _display_bounds_dict(metadata),
            "density_points_per_m2": density,
            "acquisition": metadata.acquisition,
            "sha256": digest,
            "file_size_bytes": size_bytes,
            "retained_for_processing": retained_at is not None,
            "validation": {
                "is_valid": validation.is_valid,
                "problems": validation.problems,
                "warnings": validation.warnings,
            },
            "jobs": [
                {
                    "id": j["id"],
                    "job_type": j["job_type"],
                    "status": j["status"],
                    "detail": j["detail"],
                }
                for j in jobs
            ],
            "note": (
                "Point cloud inspected from its header: CRS, extent and point "
                "count. Building extraction is available via "
                "/point-clouds/{source_id}/extract and storey segmentation via "
                "/point-clouds/{source_id}/segment-floors. Both are algorithmic, "
                "not machine learning."
            ),
        }
    finally:
        # The spool file was moved into the store on success; clean up the
        # temporary directory either way.
        try:
            handle_path.unlink(missing_ok=True)
            handle_path.parent.rmdir()
        except OSError:
            pass


def list_processing_jobs(
    repo: CadastreRepository, source_id: str | None = None
) -> list[dict[str, Any]]:
    """Processing jobs, newest last, optionally filtered by source."""
    jobs = repo.records("processing_jobs")
    if source_id:
        jobs = [j for j in jobs if j.get("source_id") == source_id]
    return sorted(jobs, key=lambda j: (str(j.get("started_at", "")), str(j.get("id", ""))))


__all__ = [
    "PIPELINE_STAGES",
    "IngestionFailure",
    "ProcessingJob",
    "ingest_point_cloud",
    "list_processing_jobs",
]
