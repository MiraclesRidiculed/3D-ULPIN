"""Runs floor segmentation over extracted buildings and stores the result.

For each building produced by :mod:`app.services.building_extraction_service`,
this re-reads the point cloud, isolates that building's points, detects its
storey levels from the elevation data, segments them, and stores the storeys.

**Storeys only.** No property volumes, no ownership, no identifiers. The
``extracted_floors`` table has no column that could hold any of those, and
nothing here creates a cadastral record.

What the output contains
------------------------
``floor_number``, ``z_min``/``z_max`` (absolute elevation in the point cloud's
projected CRS, plus the heights above the building base), the storey's plan
geometry, a ``geometric_quality`` regularity score, ``source_id`` and
``processing_job_id`` -- and ``requires_human_review`` with specific reasons
whenever the segmentation is not something the points establish on their own.
"""
from __future__ import annotations

import uuid
from typing import Any

import numpy as np

from app.models.enums import (
    AuditAction,
    JobStatus,
    ProcessingJobType,
    ProvenanceStage,
)
from app.models.schemas import ExtractedFloorRecord, FloorSegmentationResult
from app.repositories.base import CadastreRepository
from app.services import provenance
from app.services.audit import create_audit_event
from app.services import building_extraction_service as extraction_service
from app.services import crs as crs_service
from app.services import geometry as geometry_service
from app.services import point_cloud_floors as floors_engine
from app.services import point_cloud_read as pc_read
from app.services import point_cloud_store as pc_store
from app.utils import now

#: Reported alongside every result so the method is never ambiguous.
DISCLAIMER = (
    "Algorithmic/geometric floor segmentation. Storey levels are derived from the "
    "point elevation histogram, not from any assumed storey height or count. No "
    "machine-learning model was trained or applied, and no accuracy figure is "
    "claimed. Storeys only: no property volumes or ownership records are created."
)


class FloorSegmentationFailure(Exception):
    """Segmentation could not run. Carries an HTTP-facing status code."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = message


def _footprint_for(record: dict[str, Any], crs: str) -> Any:
    """An extracted building's plan geometry, in the processing CRS.

    The stored footprint is WGS84, so it is read as degrees and reprojected into
    the scan's CRS -- once. ``geojson_to_polygon`` would convert it into the local
    plane instead, and reprojecting that would move the building.
    """
    geo = record.get("footprint")
    if not geo:
        return None
    polygon = geometry_service.wgs84_geojson_to_polygon(geo)
    return crs_service.transform_geometry(polygon, "EPSG:4326", crs)


def _points_within(
    x: np.ndarray, y: np.ndarray, footprint: Any, margin: float = 1.0
) -> np.ndarray:
    """Mask of the points belonging to one building's footprint."""
    if footprint is None or footprint.is_empty:
        return np.ones(x.size, dtype=bool)
    min_x, min_y, max_x, max_y = footprint.bounds
    return (
        (x >= min_x - margin)
        & (x <= max_x + margin)
        & (y >= min_y - margin)
        & (y <= max_y + margin)
    )


def segment_building_floors(
    repo: CadastreRepository,
    source_id: str,
    *,
    building_id: str | None = None,
    floor_height: float | None = None,
    chunk_points: int = pc_read.DEFAULT_CHUNK_POINTS,
    created_by: str | None = None,
) -> FloorSegmentationResult:
    """Segment storeys for a source's extracted buildings and persist them.

    ``floor_height`` may be supplied when the caller already knows it -- from a
    survey record, say. Otherwise the storey height is estimated from the points,
    and the result records which happened, because a caller-supplied height is not
    a measurement and must not be presented as one.
    """
    source = extraction_service.find_point_cloud_source(repo, source_id)
    if source is None:
        raise FloorSegmentationFailure(f"No point cloud source {source_id!r}", 404)

    metadata = source.get("metadata") or {}
    crs = source.get("crs") or crs_service.UNKNOWN_CRS
    if crs == crs_service.UNKNOWN_CRS:
        _fail_stage(
            repo,
            source_id,
            f"Source {source_id} declares no CRS, so its points cannot be "
            "interpreted. Re-upload with ?crs=EPSG:<code>.",
        )
        raise FloorSegmentationFailure(
            f"Source {source_id} declares no CRS, so its points cannot be "
            "interpreted. Re-upload with ?crs=EPSG:<code>.",
            422,
        )

    stored_relative = metadata.get("point_cloud_storage_path")
    path = pc_store.resolve_stored(stored_relative) if stored_relative else None
    if path is None:
        _fail_stage(
            repo, source_id, f"Source {source_id} has no readable retained file."
        )
        raise FloorSegmentationFailure(
            f"Source {source_id} has no retained file to segment. Re-upload the "
            "file.",
            409,
        )

    buildings = extraction_service.list_extracted_buildings(repo, source_id)
    if building_id:
        buildings = [b for b in buildings if b.get("id") == building_id]
    if not buildings:
        _fail_stage(
            repo,
            source_id,
            "No extracted building to segment; run building extraction first.",
        )
        raise FloorSegmentationFailure(
            f"Source {source_id} has no extracted building to segment. Run "
            "POST /point-clouds/{id}/extract first.",
            409,
        )

    job_id = f"JOB-{uuid.uuid4().hex[:12]}"
    started = now()
    repo.add(
        "processing_jobs",
        {
            "id": job_id,
            "source_id": source_id,
            "job_type": ProcessingJobType.FLOOR_SEGMENTATION.value,
            "status": JobStatus.RUNNING.value,
            "point_count": metadata.get("point_count"),
            "crs": crs,
            "detail": "Reading points and segmenting storeys",
            "started_at": started,
            "created_by": created_by,
            "metadata": {"method": floors_engine.SEGMENTATION_METHOD},
        },
    )

    try:
        points = pc_read.read_points(
            path,
            source.get("filename") or path.name,
            crs=crs,
            source_id=source_id,
            chunk_points=chunk_points,
        )
    except pc_read.PointReadError as exc:
        _fail_stage(repo, source_id, exc.detail)
        raise FloorSegmentationFailure(exc.detail, exc.status_code) from None

    warnings: list[str] = []
    if points.truncated:
        warnings.append(
            "Read hit the point cap, so segmentation may have missed structure in "
            "the unread remainder."
        )
    if not points.read_in_chunks:
        warnings.append(
            f"{points.format} has no streaming reader, so the whole vertex block "
            "was loaded into memory for this run."
        )

    stored: list[dict[str, Any]] = []
    per_building: list[dict[str, Any]] = []
    review_reasons: list[str] = []

    for record in buildings:
        processing_crs = points.processing_crs or points.crs
        footprint = _footprint_for(record, processing_crs)
        mask = _points_within(points.x, points.y, footprint)
        bx, by, bz = points.x[mask], points.y[mask], points.z[mask]
        if bx.size == 0:
            warnings.append(
                f"Building {record.get('id')} has no points inside its footprint; "
                "it was skipped"
            )
            continue

        base_z = _base_datum(bx, by, bz, footprint)
        segmentation = floors_engine.segment_floor_points(
            bx,
            by,
            bz,
            base_z=base_z,
            building_footprint=footprint,
            crs=processing_crs,
            floor_height=floor_height,
            source_id=source_id,
            processing_job_id=job_id,
        )
        roles = {
            "source_crs": points.source_crs or crs,
            "processing_crs": processing_crs,
            "display_crs": points.display_crs,
        }
        segmentation.provenance.update(roles)
        for storey in segmentation.floors:
            storey.provenance.update(roles)
        review_reasons.extend(segmentation.review_reasons)

        for storey in segmentation.floors:
            record_out = _storey_record(
                storey,
                source_id=source_id,
                job_id=job_id,
                building_id=str(record.get("id")),
                crs=processing_crs,
            )
            stored.append(repo.add("extracted_floors", record_out))
            # Recorded as the storey is created. The upstream extracted building
            # is kept in ``parameters.derived_from`` because a storey has two
            # parents -- its job and its building -- and one ``parent_id`` cannot
            # hold both. No model: this is an elevation-histogram peak search.
            provenance.link_pipeline_output(
                repo,
                stage=ProvenanceStage.FLOOR.value,
                object_id=record_out["id"],
                source_id=source_id,
                processing_job_id=job_id,
                parent_id=str(record.get("id")),
                parent_stage=ProvenanceStage.BUILDING.value,
                algorithm=floors_engine.SEGMENTATION_METHOD,
                method_description=floors_engine.METHOD_DESCRIPTION,
                created_by=created_by,
            )

        per_building.append(
            {
                "building_id": record.get("id"),
                "building_height_m": record.get("height_m"),
                "floor_count": segmentation.floor_count,
                "floor_height_m": round(segmentation.levels.floor_height, 3),
                "levels_from": segmentation.levels.levels_from,
                "height_estimate": segmentation.estimate.to_dict(),
                "levels": segmentation.levels.to_dict(),
                "spacing": segmentation.spacing.to_dict(),
                "requires_human_review": segmentation.requires_human_review,
                "review_reasons": segmentation.review_reasons,
                "base_z": round(base_z, 3),
            }
        )

    detail = (
        f"Algorithmic storey segmentation produced {len(stored)} store"
        f"{'' if len(stored) == 1 else 'ys'} across {len(per_building)} building"
        f"{'' if len(per_building) == 1 else 's'}"
    )
    flagged = sorted({r for r in review_reasons})
    repo.update(
        "processing_jobs",
        job_id,
        {
            "status": JobStatus.COMPLETED.value,
            "detail": detail,
            "completed_at": now(),
            "bounds": metadata.get("display_bounds"),
            "metadata": {
                "method": floors_engine.SEGMENTATION_METHOD,
                "method_description": floors_engine.METHOD_DESCRIPTION,
                "storeys_found": len(stored),
                "buildings_segmented": len(per_building),
                "points_read": int(points.x.size),
                "source_crs": points.source_crs or crs,
                "processing_crs": points.processing_crs or points.crs,
                "display_crs": points.display_crs,
                "requires_human_review": bool(flagged),
                "review_reasons": flagged,
                "warnings": warnings,
            },
        },
    )
    _close_pending_siblings(repo, source_id, job_id, detail)
    create_audit_event(
        repo,
        action=AuditAction.PROCESSED.value,
        object_id=source_id,
        object_type="DATA_SOURCE",
        actor=created_by,
        detail=detail,
        extra={
            "stage": ProcessingJobType.FLOOR_SEGMENTATION.value,
            "processing_job_id": job_id,
            "storeys_found": len(stored),
            "source_crs": points.source_crs or crs,
            "processing_crs": points.processing_crs or points.crs,
            "display_crs": points.display_crs,
            "method": floors_engine.SEGMENTATION_METHOD,
            "requires_human_review": bool(flagged),
        },
    )

    return FloorSegmentationResult(
        source_id=source_id,
        processing_job_id=job_id,
        method=floors_engine.SEGMENTATION_METHOD,
        method_description=floors_engine.METHOD_DESCRIPTION,
        point_cloud=pc_read.point_set_summary(points),
        buildings_segmented=len(per_building),
        storeys_found=len(stored),
        storeys=[ExtractedFloorRecord(**_as_schema(s)) for s in stored],
        buildings=per_building,
        requires_human_review=bool(flagged),
        review_reasons=flagged,
        warnings=warnings,
        note=DISCLAIMER,
    )


def _base_datum(
    x: np.ndarray, y: np.ndarray, z: np.ndarray, footprint: Any
) -> float:
    """The elevation a building's storey 1 sits on.

    A low percentile of the building's own points, so the base is the underside
    of the structure rather than an average of it.
    """
    if footprint is not None and not footprint.is_empty:
        cx = (footprint.bounds[0] + footprint.bounds[2]) / 2.0
        cy = (footprint.bounds[1] + footprint.bounds[3]) / 2.0
    else:
        cx, cy = float(np.median(x)), float(np.median(y))
    near = (np.abs(x - cx) <= 2.0) & (np.abs(y - cy) <= 2.0)
    if near.any():
        return float(np.percentile(z[near], 2.0))
    return float(np.percentile(z, 2.0))


def _storey_record(
    storey: floors_engine.ExtractedFloor,
    *,
    source_id: str,
    job_id: str,
    building_id: str,
    crs: str,
) -> dict[str, Any]:
    """A storey as a persistable record, with the footprint reprojected once."""
    wgs84 = crs_service.transform_geometry(
        storey.footprint, crs, geometry_service.GEOGRAPHIC_CRS
    )
    return {
        # The full building id, not its last dash-separated segment. An
        # extracted building id *ends* in its ordinal ("XB-{job}-001"), so that
        # segment is "001" for the first building of every job -- and a single
        # segmentation job can be handed buildings left by several extraction
        # jobs. Keying on it produced the same storey primary key twice: a silent
        # duplicate row in memory, and a UniqueViolation/500 under PostGIS.
        "id": f"XF-{job_id}-{building_id}-{storey.floor_number:03d}",
        "source_id": source_id,
        "processing_job_id": job_id,
        "building_id": building_id,
        "floor_number": storey.floor_number,
        "z_min": storey.z_min,
        "z_max": storey.z_max,
        "z_min_above_ground": storey.z_min_above_ground,
        "z_max_above_ground": storey.z_max_above_ground,
        "footprint": {
            "type": wgs84.geom_type,
            "coordinates": [[list(c) for c in wgs84.exterior.coords]],
        },
        "geometry_3d": {
            "z_min": storey.z_min,
            "z_max": storey.z_max,
            "footprint": {
                "type": wgs84.geom_type,
                "coordinates": [[list(c) for c in wgs84.exterior.coords]],
            },
        },
        "area_m2": storey.area_m2,
        "volume_m3": storey.volume_m3,
        "crs": crs,
        "method": storey.method,
        "geometry_hash": storey.geometry_hash,
        "geometric_quality": storey.geometric_quality,
        "requires_human_review": storey.requires_human_review,
        "segmentation": {
            "method_description": floors_engine.METHOD_DESCRIPTION,
            "quality_metrics": storey.quality_metrics,
            "review_reasons": storey.review_reasons,
            "provenance": storey.provenance,
        },
        "created_at": now(),
    }


def present_storey(record: dict[str, Any]) -> dict[str, Any]:
    """Flatten a stored storey into the API shape."""
    segmentation = record.get("segmentation") or {}
    presented = dict(record)
    presented.setdefault("quality_metrics", segmentation.get("quality_metrics") or {})
    presented.setdefault("review_reasons", segmentation.get("review_reasons") or [])
    presented.setdefault("provenance", segmentation.get("provenance") or {})
    return presented


def _as_schema(record: dict[str, Any]) -> dict[str, Any]:
    allowed = set(ExtractedFloorRecord.model_fields)
    return {k: v for k, v in present_storey(record).items() if k in allowed}


def list_extracted_floors(
    repo: CadastreRepository,
    *,
    source_id: str | None = None,
    building_id: str | None = None,
) -> list[dict[str, Any]]:
    """Storeys, optionally filtered by source or building."""
    records = repo.records("extracted_floors")
    if source_id:
        records = [r for r in records if r.get("source_id") == source_id]
    if building_id:
        records = [r for r in records if r.get("building_id") == building_id]
    return [present_storey(r) for r in records]


def _fail_stage(repo: CadastreRepository, source_id: str, error: str) -> None:
    """Mark the outstanding segmentation stage failed, in its own transaction.

    A failed request rolls its session back, so recording the failure through the
    request's repository would discard it.
    """
    from app.db.session import session_scope, uses_postgres
    from app.repositories.postgres import PostgresCadastreRepository

    def apply(target: CadastreRepository) -> None:
        for job in target.records("processing_jobs"):
            if (
                job.get("source_id") == source_id
                and job.get("job_type")
                == ProcessingJobType.FLOOR_SEGMENTATION.value
                and job.get("status") == JobStatus.PENDING.value
            ):
                target.update(
                    "processing_jobs",
                    job["id"],
                    {
                        "status": JobStatus.FAILED.value,
                        "error": error,
                        "detail": "Floor segmentation failed",
                        "completed_at": now(),
                    },
                )

    if uses_postgres():
        with session_scope() as session:
            apply(PostgresCadastreRepository(session))
    else:
        apply(repo)


def _close_pending_siblings(
    repo: CadastreRepository, source_id: str, exclude: str, detail: str
) -> int:
    """Complete every ``PENDING`` segmentation job for a source."""
    closed = 0
    for job in repo.records("processing_jobs"):
        if (
            job.get("source_id") == source_id
            and job.get("job_type") == ProcessingJobType.FLOOR_SEGMENTATION.value
            and job.get("status") == JobStatus.PENDING.value
            and job.get("id") != exclude
        ):
            repo.update(
                "processing_jobs",
                job["id"],
                {
                    "status": JobStatus.COMPLETED.value,
                    "detail": detail,
                    "completed_at": now(),
                },
            )
            closed += 1
    return closed


__all__ = [
    "DISCLAIMER",
    "FloorSegmentationFailure",
    "list_extracted_floors",
    "present_storey",
    "segment_building_floors",
]
