"""Runs building extraction over an ingested point cloud and stores the result.

Ties the pieces together: resolve the retained upload, read its points, run the
extractor, persist the buildings, and record the outcome on a processing job.

**Algorithmic / geometric, not machine learning.** The result carries
``method="algorithmic_geometric"`` and a ``geometric_quality`` regularity score.
No accuracy or confidence figure is produced, because none can honestly be
produced without a labelled dataset to measure against.
"""
from __future__ import annotations

import uuid
from typing import Any

from app.models.enums import (
    AuditAction,
    JobStatus,
    ProcessingJobType,
    ProvenanceStage,
)
from app.models.schemas import BuildingExtractionResult, ExtractedBuilding
from app.repositories.base import CadastreRepository
from app.services import provenance
from app.services.audit import create_audit_event
from app.services import crs as crs_service
from app.services import geometry as geometry_service
from app.services import point_cloud_read as pc_read
from app.services import point_cloud_store as pc_store
from app.services.point_cloud_extraction import (
    EXTRACTION_METHOD,
    EXTRACTOR_VERSION,
    METHOD_DESCRIPTION,
    BuildingExtractor,
    DeterministicBuildingExtractor,
    ExtractionParams,
)
from app.utils import now

#: Reported alongside every result so the method is never ambiguous.
DISCLAIMER = (
    "Algorithmic/geometric extraction. No machine-learning model was trained or "
    "applied, and no accuracy figure is claimed; geometric_quality measures "
    "geometric regularity only."
)


class ExtractionFailure(Exception):
    """Extraction could not run. Carries an HTTP-facing status code."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = message


def find_point_cloud_source(
    repo: CadastreRepository, source_id: str
) -> dict[str, Any] | None:
    """The source row for ``source_id``, if it is a retained point cloud."""
    for source in repo.records("sources"):
        if source.get("id") != source_id:
            continue
        metadata = source.get("metadata") or {}
        if metadata.get("point_cloud"):
            return source
    return None


def _geojson_footprint(footprint: Any, crs: str) -> dict[str, Any]:
    """Footprint as WGS84 GeoJSON, for storage in the authoritative CRS.

    The scan's coordinates are in its own projected CRS, so they are reprojected
    once here. Every geometry column in this schema is WGS84.

    Built as a plain GeoJSON mapping rather than via
    :func:`~app.services.geometry.polygon_to_geojson`, which applies the
    *local-plane* to WGS84 transform and would reproject a second time -- sending
    the footprint hundreds of kilometres away and collapsing it to a point.
    """
    if crs_service.is_metric(crs):
        wgs84 = crs_service.transform_geometry(
            footprint, crs, geometry_service.GEOGRAPHIC_CRS
        )
    else:
        wgs84 = footprint
    return {
        "type": wgs84.geom_type,
        "coordinates": [[list(coord) for coord in wgs84.exterior.coords]],
    }


def _footprint_area(record: dict[str, Any]) -> float | None:
    """Footprint area in mÂ² for a stored building, or ``None`` if unavailable.

    The stored footprint is WGS84, so its area must be measured in a projected
    metre CRS. The value measured during extraction is preferred, since it was
    computed from the scan's own projected coordinates and needs no reprojection.
    """
    components = (record.get("extraction") or {}).get("quality_metrics", {}).get(
        "components", {}
    )
    area = components.get("footprint_area_m2")
    if area is not None:
        return float(area)
    footprint = record.get("footprint")
    if not footprint:
        return None
    try:
        # The stored footprint is WGS84, so it must be read as degrees --
        # ``geojson_to_polygon`` would convert it into the local plane and the
        # area would then be reprojected a second time.
        polygon = geometry_service.wgs84_geojson_to_polygon(footprint)
        return crs_service.calculate_metric_area(polygon, source_crs="EPSG:4326")
    except Exception:  # noqa: BLE001 - a summary must not fail the run
        return None


def _quality_summary(buildings: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-building quality components across a run."""
    if not buildings:
        return {
            "buildings": 0,
            "note": "No buildings were extracted from this point cloud.",
        }
    qualities = [b["geometric_quality"] for b in buildings]
    # Read from the record's own extraction blob, with a footprint-area fallback
    # so the total does not depend on how deeply quality detail is nested.
    areas = [
        value
        for b in buildings
        if (value := _footprint_area(b)) is not None
    ]
    return {
        "buildings": len(buildings),
        "geometric_quality_mean": round(sum(qualities) / len(qualities), 4),
        "geometric_quality_min": round(min(qualities), 4),
        "geometric_quality_max": round(max(qualities), 4),
        "total_footprint_area_m2": round(sum(areas), 2) if areas else None,
        "interpretation": (
            "geometric_quality summarises roof flatness, point density, footprint "
            "compactness and height stability. It is a regularity score, not an "
            "accuracy figure and not a probability of correctness."
        ),
    }


def extract_buildings(
    repo: CadastreRepository,
    source_id: str,
    *,
    extractor: BuildingExtractor | None = None,
    params: ExtractionParams | None = None,
    chunk_points: int = pc_read.DEFAULT_CHUNK_POINTS,
    max_points: int | None = None,
    created_by: str | None = None,
) -> BuildingExtractionResult:
    """Extract buildings from an ingested point cloud and persist them.

    Raises :class:`ExtractionFailure` with an HTTP status when the cloud is
    missing, has no retained file, declares no CRS, or cannot be read.
    """
    extractor = extractor or DeterministicBuildingExtractor(params)
    params = params or getattr(extractor, "params", None) or ExtractionParams()

    source = find_point_cloud_source(repo, source_id)
    if source is None:
        raise ExtractionFailure(f"No point cloud source {source_id!r}", 404)

    metadata = source.get("metadata") or {}
    crs = source.get("crs") or crs_service.UNKNOWN_CRS
    if crs == crs_service.UNKNOWN_CRS:
        # The stage is marked FAILED rather than left PENDING, so a cloud that
        # cannot be processed is visibly not processable.
        _record_extraction_failure(
            repo,
            source_id,
            f"Source {source_id} declares no CRS, so its coordinates cannot be "
            "interpreted. Re-upload with ?crs=EPSG:<code>.",
        )
        raise ExtractionFailure(
            f"Source {source_id} declares no CRS, so its coordinates cannot be "
            "interpreted. Re-upload with ?crs=EPSG:<code>.",
            422,
        )

    stored_relative = metadata.get("point_cloud_storage_path")
    if not stored_relative:
        _record_extraction_failure(
            repo, source_id, f"Source {source_id} has no retained file to extract from."
        )
        raise ExtractionFailure(
            f"Source {source_id} has no retained file. Point clouds are retained "
            "on disk at upload time; re-upload the file to extract from it.",
            409,
        )
    path = pc_store.resolve_stored(stored_relative)
    if path is None:
        _record_extraction_failure(
            repo, source_id, f"Retained file for {source_id} is missing."
        )
        raise ExtractionFailure(
            f"Retained file for {source_id} is missing from the point-cloud store. "
            "Re-upload the file.",
            409,
        )

    filename = source.get("filename") or path.name

    # A job is opened up front so a failure is recorded rather than vanishing.
    job_id = f"JOB-{uuid.uuid4().hex[:12]}"
    started = now()
    repo.add(
        "processing_jobs",
        {
            "id": job_id,
            "source_id": source_id,
            "job_type": ProcessingJobType.BUILDING_EXTRACTION.value,
            "status": JobStatus.RUNNING.value,
            "point_count": metadata.get("point_count"),
            "crs": crs,
            "detail": "Reading points and running algorithmic extraction",
            "started_at": started,
            "created_by": created_by,
            "metadata": {"extractor": extractor.name, "method": extractor.method},
        },
    )

    try:
        points = pc_read.read_points(
            path,
            filename,
            crs=crs,
            source_id=source_id,
            chunk_points=chunk_points,
            **({"max_points": max_points} if max_points else {}),
        )
    except pc_read.PointReadError as exc:
        _record_extraction_failure(repo, source_id, exc.detail)
        raise ExtractionFailure(exc.detail, exc.status_code) from None

    buildings = extractor.extract(
        points, source_id=source_id, processing_job_id=job_id, crs=crs
    )

    warnings: list[str] = []
    if points.truncated:
        warnings.append(
            f"Read stopped at the {max_points or pc_read.DEFAULT_MAX_POINTS}-point "
            "cap, so this extraction covers only part of the cloud. Raise max_points "
            "to cover it all."
        )
    if not points.read_in_chunks:
        # PLY only: plyfile has no streaming reader, so the whole vertex block is
        # resident for the run. LAS and LAZ stream, lazrs included.
        warnings.append(
            f"{points.format} has no streaming reader, so the whole vertex block "
            "was loaded into memory for this run."
        )

    stored: list[dict[str, Any]] = []
    for index, building in enumerate(buildings, start=1):
        record = building.to_record()
        # Keyed on the job, not just the source: re-running extraction for a
        # source is a normal thing to do (a re-survey, a parameter change), and
        # the new run's buildings must not collide with the previous run's rows.
        record["id"] = f"XB-{job_id}-{index:03d}"
        record["footprint"] = _geojson_footprint(building.footprint, crs)
        record["created_at"] = now()
        stored.append(repo.add("extracted_buildings", record))
        # Provenance is recorded here, at the point the object comes into
        # existence, rather than inferred afterwards. ``model_name`` is left empty
        # because nothing in this pipeline is a model -- see
        # ``services/provenance.py``.
        provenance.link_pipeline_output(
            repo,
            stage=ProvenanceStage.BUILDING.value,
            object_id=record["id"],
            source_id=source_id,
            processing_job_id=job_id,
            parent_id=job_id,
            parent_stage=ProvenanceStage.PROCESSING_JOB.value,
            algorithm=extractor.method,
            method_description=METHOD_DESCRIPTION,
            parameters=params.to_dict(),
            created_by=created_by,
        )

    detail = (
        f"Algorithmic extraction produced {len(stored)} building"
        f"{'' if len(stored) == 1 else 's'}"
    )
    repo.update(
        "processing_jobs",
        job_id,
        {
            "status": JobStatus.COMPLETED.value,
            "detail": detail,
            "completed_at": now(),
            "bounds": metadata.get("display_bounds"),
            "metadata": {
                "extractor": extractor.name,
                "extractor_version": EXTRACTOR_VERSION,
                "method": extractor.method,
                "method_description": METHOD_DESCRIPTION,
                "buildings_found": len(stored),
                "points_read": int(points.x.size),
                "quality_summary": _quality_summary(stored),
                "warnings": warnings,
            },
        },
    )

    # Every outstanding row for this stage is now satisfied. All of them, not
    # just the first: repeated ingests leave more than one, and a single stale
    # PENDING would keep the source reading as METADATA_ONLY forever.
    _close_pending_siblings(repo, source_id, job_id, detail)
    create_audit_event(
        repo,
        action=AuditAction.PROCESSED.value,
        object_id=source_id,
        object_type="DATA_SOURCE",
        actor=created_by,
        detail=detail,
        extra={
            "stage": ProcessingJobType.BUILDING_EXTRACTION.value,
            "processing_job_id": job_id,
            "buildings_found": len(stored),
            "extractor": extractor.name,
            "extractor_version": EXTRACTOR_VERSION,
            "method": extractor.method,
            "points_read": int(points.x.size),
        },
    )

    return BuildingExtractionResult(
        source_id=source_id,
        processing_job_id=job_id,
        method=extractor.method,
        method_description=METHOD_DESCRIPTION,
        extractor=extractor.name,
        extractor_version=EXTRACTOR_VERSION,
        point_cloud=pc_read.point_set_summary(points),
        buildings_found=len(stored),
        buildings=[ExtractedBuilding(**_as_schema(s)) for s in stored],
        quality_summary=_quality_summary(stored),
        warnings=warnings,
        parameters=params.to_dict(),
        note=DISCLAIMER,
    )


def _close_pending_siblings(
    repo: CadastreRepository, source_id: str, exclude: str, detail: str
) -> int:
    """Complete every ``PENDING`` extraction job for a source. Returns the count.

    Ingestion records the stage as ``PENDING``; once a real run succeeds those
    rows must stop claiming the work is outstanding. Repeated ingests of the same
    file leave more than one, so all are closed.
    """
    closed = 0
    for job in repo.records("processing_jobs"):
        if (
            job.get("source_id") == source_id
            and job.get("job_type") == ProcessingJobType.BUILDING_EXTRACTION.value
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


def _record_extraction_failure(
    repo: CadastreRepository, source_id: str, error: str
) -> None:
    """Mark a source's outstanding extraction job ``FAILED``, with the reason.

    Committed in its **own transaction**, because a failed request rolls its own
    session back: recording the failure through the request's repository would
    discard it and leave the stage looking available forever.

    Only the ingestion-time ``PENDING`` row is marked. A job opened by the failing
    run is part of the rolled-back transaction and does not exist afterwards,
    which is the correct outcome -- nothing half-written survives.
    """
    from app.db.session import session_scope, uses_postgres
    from app.repositories.postgres import PostgresCadastreRepository

    def apply(target: CadastreRepository) -> None:
        for job in target.records("processing_jobs"):
            if (
                job.get("source_id") == source_id
                and job.get("job_type")
                == ProcessingJobType.BUILDING_EXTRACTION.value
                and job.get("status") == JobStatus.PENDING.value
            ):
                target.update(
                    "processing_jobs",
                    job["id"],
                    {
                        "status": JobStatus.FAILED.value,
                        "error": error,
                        "detail": "Extraction failed",
                        "completed_at": now(),
                    },
                )

    if uses_postgres():
        with session_scope() as session:
            apply(PostgresCadastreRepository(session))
    else:
        # The in-memory store has no transactions, so the request's repository
        # is already durable.
        apply(repo)


def present_building(record: dict[str, Any]) -> dict[str, Any]:
    """Flatten a stored record into the API's building shape.

    Quality components and provenance are stored together in one ``extraction``
    JSONB column (a single JSON column per collection keeps the two backends
    byte-identical), and are expanded here for presentation.
    """
    extraction = record.get("extraction") or {}
    presented = dict(record)
    presented.setdefault("quality_metrics", extraction.get("quality_metrics") or {})
    presented.setdefault("provenance", extraction.get("provenance") or {})
    return presented


def _as_schema(record: dict[str, Any]) -> dict[str, Any]:
    """Present a stored record as the declared schema, dropping extras."""
    allowed = set(ExtractedBuilding.model_fields)
    return {k: v for k, v in present_building(record).items() if k in allowed}


def list_extracted_buildings(
    repo: CadastreRepository, source_id: str | None = None
) -> list[dict[str, Any]]:
    """Extracted buildings, optionally filtered to one source."""
    records = repo.records("extracted_buildings")
    if source_id:
        records = [r for r in records if r.get("source_id") == source_id]
    # Filtered to the declared schema, exactly as the extraction response is.
    # Returning the raw stored record here gave one object two shapes depending
    # on which endpoint read it, so a client could not tell whether a field was
    # missing or simply not part of the contract.
    return [_as_schema(r) for r in records]


def source_extraction_status(
    repo: CadastreRepository, source_id: str
) -> dict[str, Any]:
    """How far a point cloud has been processed, for the UI's status column.

    Derived from the jobs table rather than copied onto the source, so it cannot
    drift from what actually ran.
    """
    jobs = [
        j
        for j in repo.records("processing_jobs")
        if j.get("source_id") == source_id
    ]
    by_type: dict[str, list[str]] = {}
    for job in jobs:
        by_type.setdefault(str(job.get("job_type")), []).append(
            str(job.get("status"))
        )

    metadata = by_type.get(ProcessingJobType.METADATA_EXTRACTION.value, [])
    extraction = by_type.get(ProcessingJobType.BUILDING_EXTRACTION.value, [])
    segmentation = by_type.get(ProcessingJobType.FLOOR_SEGMENTATION.value, [])

    if any(s == JobStatus.FAILED.value for s in extraction):
        stage = "EXTRACTION_FAILED"
    elif any(s == JobStatus.RUNNING.value for s in extraction):
        stage = "EXTRACTING"
    elif any(s == JobStatus.COMPLETED.value for s in extraction):
        stage = "EXTRACTED"
    elif any(s == JobStatus.PENDING.value for s in extraction):
        stage = "METADATA_ONLY"
    elif any(s == JobStatus.COMPLETED.value for s in metadata):
        stage = "METADATA_ONLY"
    else:
        stage = "UNKNOWN"

    return {
        "stage": stage,
        "metadata": _summarise(metadata),
        "building_extraction": _summarise(extraction),
        "floor_segmentation": _summarise(segmentation),
        "floor_segmentation_note": (
            "Not implemented in this milestone; no storey detection has been run."
            if any(s == JobStatus.NOT_IMPLEMENTED.value for s in segmentation)
            else None
        ),
    }


def _summarise(statuses: list[str]) -> str:
    """Roll a stage's job statuses into one headline."""
    if not statuses:
        return "UNKNOWN"
    if JobStatus.FAILED.value in statuses:
        return JobStatus.FAILED.value
    if JobStatus.RUNNING.value in statuses:
        return JobStatus.RUNNING.value
    if JobStatus.COMPLETED.value in statuses:
        return JobStatus.COMPLETED.value
    if JobStatus.PENDING.value in statuses:
        return JobStatus.PENDING.value
    return JobStatus.NOT_IMPLEMENTED.value


__all__ = [
    "DISCLAIMER",
    "EXTRACTION_METHOD",
    "ExtractionFailure",
    "extract_buildings",
    "find_point_cloud_source",
    "list_extracted_buildings",
    "present_building",
    "source_extraction_status",
]
