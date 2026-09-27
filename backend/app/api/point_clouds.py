"""Point-cloud endpoints.

Listing and metadata views are **header-derived**: they read the record written
at upload time and never open a point cloud.

The one exception is ``POST /point-clouds/{id}/extract``, which genuinely reads
points, because that is what extraction requires.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import request_repository
from app.repositories.base import CadastreRepository
from app.services import building_extraction_service as extraction_service
from app.services import floor_segmentation_service as floor_service
from app.services import property_volume_service as volume_service
from app.services.crs import UNKNOWN_CRS
from app.services.point_cloud_ingestion import list_processing_jobs

router = APIRouter(tags=["point-clouds"])


def _is_point_cloud(source: dict[str, Any]) -> bool:
    metadata = source.get("metadata")
    return bool(isinstance(metadata, dict) and metadata.get("point_cloud"))


def _jobs_by_source(repository: CadastreRepository) -> dict[str, list[dict[str, Any]]]:
    """Index processing jobs by source id, once per request.

    Jobs are read from their own table rather than copied into the source's
    metadata, so status cannot go stale.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for job in list_processing_jobs(repository):
        index.setdefault(job.get("source_id", ""), []).append(job)
    return index


def _summary(
    source: dict[str, Any], jobs: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Flatten a stored source into what the UI needs to render a row."""
    metadata = source.get("metadata") or {}
    return {
        "source_id": source.get("id"),
        "filename": source.get("filename"),
        "type": source.get("source_type"),
        "format": metadata.get("format"),
        "compressed": metadata.get("compressed"),
        "crs": source.get("crs") or UNKNOWN_CRS,
        "crs_declared": metadata.get("crs_declared_in_payload", False),
        "point_count": metadata.get("point_count"),
        "density_points_per_m2": metadata.get("density_points_per_m2"),
        "bounds": metadata.get("display_bounds"),
        "source_bounds": metadata.get("source_bounds"),
        "acquisition": metadata.get("acquisition") or {},
        "sha256": metadata.get("sha256"),
        "file_size_bytes": metadata.get("file_size_bytes"),
        "validation": metadata.get("validation"),
        "status": _overall_status(jobs or []),
    }


@router.post("/point-clouds/{source_id}/extract")
def extract_buildings(
    source_id: str,
    max_points: int | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> Any:
    """Run algorithmic building extraction over an ingested point cloud.

    Reads the retained upload's points, extracts footprints and heights, and
    stores the result. This is a **geometric** pipeline -- no model is trained or
    applied, and the returned ``geometric_quality`` is a regularity score, not an
    accuracy figure.

    This endpoint produces buildings only. Storeys come from
    `POST /point-clouds/{id}/segment-floors`, which measures levels from the
    point elevations rather than assuming them.
    """
    try:
        return extraction_service.extract_buildings(
            repository, source_id, max_points=max_points
        )
    except extraction_service.ExtractionFailure as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


@router.get("/extracted-buildings")
def list_extracted(
    source_id: str | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Every building extracted from point clouds, optionally per source."""
    return extraction_service.list_extracted_buildings(repository, source_id)


@router.post("/point-clouds/{source_id}/segment-floors")
def segment_floors(
    source_id: str,
    building_id: str | None = None,
    floor_height: float | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> Any:
    """Segment storeys for a source's extracted buildings.

    Storey levels are **detected from the point elevations**, not assumed: the
    elevation histogram's peak spacing gives the storey height, and the detected
    surfaces become the levels. ``floor_height`` may be supplied when the caller
    already knows it, and the result records that the levels were supplied rather
    than measured.

    This produces **storeys only** -- no property volumes, no ownership.
    Anything the points do not establish is flagged with
    ``requires_human_review`` and a specific reason.
    """
    try:
        return floor_service.segment_building_floors(
            repository, source_id, building_id=building_id, floor_height=floor_height
        )
    except floor_service.FloorSegmentationFailure as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


@router.get("/extracted-floors")
def list_storeys(
    source_id: str | None = None,
    building_id: str | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Storeys detected in point clouds, optionally per source or building."""
    return floor_service.list_extracted_floors(
        repository, source_id=source_id, building_id=building_id
    )


@router.post("/point-clouds/{source_id}/property-volumes")
def generate_property_volumes(
    source_id: str,
    building_id: str | None = None,
    ground_datum: float | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> Any:
    """Generate volumetric property rights from a parcel and the extracted storeys.

    Where floor-plan unit data has been supplied for a storey, it is divided into
    unit volumes. **Where it has not, the storey is emitted as a single volume
    marked ``volume_scope="FLOOR"``** -- a statement about the storey's extent,
    not a claim that it is one ownership unit. Apartment boundaries are never
    invented.

    This generates geometry only. No ownership, tenancy or title is asserted, and
    the cadastral ``property_volumes`` table is not written to.
    """
    try:
        return volume_service.generate_volumes(
            repository,
            source_id,
            building_id=building_id,
            ground_datum=ground_datum,
        )
    except volume_service.VolumeGenerationFailure as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


@router.get("/generated-property-volumes")
def list_property_volumes(
    source_id: str | None = None,
    building_id: str | None = None,
    parcel_id: str | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Generated property volumes, optionally per source, building or parcel."""
    return volume_service.list_generated_volumes(
        repository, source_id=source_id, building_id=building_id, parcel_id=parcel_id
    )


def _overall_status(jobs: list[dict[str, Any]]) -> str:
    """Roll the per-stage job statuses up into one headline status.

    Reported honestly. A cloud whose metadata succeeded but whose extraction has
    not run is ``METADATA_ONLY``, never ``COMPLETE``. Even after property volumes
    are generated the status is ``VOLUMES_GENERATED``, not ``COMPLETE``: this
    system derives geometry and asserts no ownership, tenancy or title, so the
    cadastral work genuinely is not finished.
    """
    if not jobs:
        return "UNKNOWN"
    by_type = {j["job_type"]: j["status"] for j in jobs}
    statuses = set(by_type.values())
    if "FAILED" in statuses:
        return "FAILED"
    if "RUNNING" in statuses:
        return "RUNNING"
    if by_type.get("PROPERTY_VOLUME_GENERATION") == "COMPLETED":
        return "VOLUMES_GENERATED"
    if by_type.get("FLOOR_SEGMENTATION") == "COMPLETED":
        return "SEGMENTED"
    if by_type.get("BUILDING_EXTRACTION") == "COMPLETED":
        return "EXTRACTED"
    if (
        by_type.get("BUILDING_EXTRACTION") == "PENDING"
        and by_type.get("METADATA_EXTRACTION") == "COMPLETED"
    ):
        return "METADATA_ONLY"
    if "COMPLETED" in statuses:
        return "METADATA_ONLY"
    return "PENDING"


@router.get("/point-clouds")
def list_point_clouds(
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Every ingested point cloud, with CRS, extent, count and status."""
    jobs = _jobs_by_source(repository)
    return [
        _summary(source, jobs.get(source.get("id", "")))
        for source in repository.records("sources")
        if _is_point_cloud(source)
    ]


@router.get("/point-clouds/{source_id}")
def get_point_cloud(
    source_id: str, repository: CadastreRepository = Depends(request_repository)
) -> dict[str, Any] | None:
    """One point cloud's metadata and its processing jobs, or ``None``."""
    for source in repository.records("sources"):
        if source.get("id") == source_id and _is_point_cloud(source):
            summary = _summary(source, list_processing_jobs(repository, source_id))
            summary["jobs"] = [
                {
                    "id": j["id"],
                    "job_type": j["job_type"],
                    "status": j["status"],
                    "detail": j.get("detail"),
                    "point_count": j.get("point_count"),
                    "crs": j.get("crs"),
                    "bounds": j.get("bounds"),
                    "error": j.get("error"),
                    "started_at": j.get("started_at"),
                    "completed_at": j.get("completed_at"),
                }
                for j in list_processing_jobs(repository, source_id)
            ]
            return summary
    return None


@router.get("/processing-jobs")
def list_jobs(
    source_id: str | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Processing jobs across all sources, or filtered to one."""
    return list_processing_jobs(repository, source_id)
