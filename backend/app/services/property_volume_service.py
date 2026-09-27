"""Runs property-volume generation over a source's extracted buildings.

Reads the extracted buildings and storeys produced by the earlier milestones,
matches each building to a parcel, and generates volumetric property rights.

**Storeys, not invented units.** Where floor-plan unit data is supplied for a
storey, the storey is divided into unit volumes. Where it is not, one volume per
storey is produced and marked ``volume_scope="FLOOR"`` with
``units_inferred=False``.

**Geometry, not rights.** The output asserts no ownership, tenancy or title.
``parent_parcel_id`` associates a volume with a parcel; it does not state who
owns what. The cadastral ``property_volumes`` table is never written to.
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
from app.models.schemas import GeneratedPropertyVolumeRecord, VolumeGenerationResult
from app.repositories.base import CadastreRepository
from app.services import provenance
from app.services.audit import create_audit_event
from app.services import building_extraction_service as extraction_service
from app.services import crs as crs_service
from app.services import floor_segmentation_service as floor_service
from app.services import property_volumes as volumes_engine
from app.services import ulpin as ulpin_service
from app.utils import now

#: Reported alongside every result so the method is never ambiguous.
DISCLAIMER = (
    "Derived geometric property-volume generation. Volumes are built from a "
    "parent parcel, an extracted building footprint and storey geometry with a "
    "vertical band. Where no floor-plan unit data is available the output is one "
    "volume per storey, marked as storey scope; apartment boundaries are never "
    "invented. No ownership, tenancy or title is asserted, and no cadastral "
    "property record is created."
)


class VolumeGenerationFailure(Exception):
    """Generation could not run. Carries an HTTP-facing status code."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = message


def _extraction_ordinal(building_id: str, fallback: int) -> int:
    """The ordinal extraction assigned this building, from ``XB-{job}-{n}``.

    Read from the id rather than from list position. Building ids are job-scoped
    (``XB-{job}-001``), so the trailing number is the building's index within its
    own extraction -- stable for a given source, and identical on every re-run,
    which is what keeps a generated volume's identity from changing when the
    pipeline is re-run. Position in a list of every building ever extracted is
    not: that list grows, so the index shifts.
    """
    tail = building_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else fallback


def generate_volumes(
    repo: CadastreRepository,
    source_id: str,
    *,
    building_id: str | None = None,
    floor_plans: dict[int, dict[str, Any]] | None = None,
    ground_datum: float | None = None,
    created_by: str | None = None,
) -> VolumeGenerationResult:
    """Generate property volumes for a source's extracted buildings.

    ``floor_plans`` carries optional per-storey unit data keyed by
    ``floor_number``. Absent or empty means undivided storeys.
    """
    source = extraction_service.find_point_cloud_source(repo, source_id)
    if source is None:
        raise VolumeGenerationFailure(f"No point cloud source {source_id!r}", 404)

    source_crs = source.get("crs") or crs_service.UNKNOWN_CRS
    metadata = source.get("metadata") or {}
    if source_crs == crs_service.UNKNOWN_CRS:
        _fail_stage(
            repo,
            source_id,
            f"Source {source_id} declares no CRS, so its geometry cannot be "
            "interpreted. Re-upload with ?crs=EPSG:<code>.",
        )
        raise VolumeGenerationFailure(
            f"Source {source_id} declares no CRS, so its geometry cannot be "
            "interpreted. Re-upload with ?crs=EPSG:<code>.",
            422,
        )

    buildings = extraction_service.list_extracted_buildings(repo, source_id)
    if building_id:
        buildings = [b for b in buildings if b.get("id") == building_id]
    if not buildings:
        _fail_stage(
            repo,
            source_id,
            "No extracted building to generate from; run building extraction first.",
        )
        raise VolumeGenerationFailure(
            f"Source {source_id} has no extracted building. Run "
            "POST /point-clouds/{id}/extract first.",
            409,
        )

    # Resolved only after the building guard, because the fallback below reads
    # ``buildings[0]``. The CRS work initially inserted this block between the
    # guard and its ``raise``, which detached the two: the guard stopped raising,
    # ``buildings[0]`` raised IndexError on an empty list, and the caller got a
    # 500 instead of the 409. Order is load-bearing, not cosmetic.
    processing_crs = metadata.get("processing_crs")
    if not processing_crs:
        # A retained PLY has no header extent, but its extracted display
        # footprint gives an explicit WGS84 anchor for selecting the metric CRS.
        footprint = buildings[0].get("footprint")
        try:
            geometry = extraction_service.geometry_service.wgs84_geojson_to_polygon(footprint)
            processing_crs = crs_service.select_processing_crs(
                geometry, source_crs=crs_service.WGS84
            )
        except Exception as exc:  # noqa: BLE001 - cannot make a metric claim
            raise VolumeGenerationFailure(
                f"Source {source_id} has no resolvable processing CRS: {exc}", 422
            ) from None

    storeys = floor_service.list_extracted_floors(repo, source_id=source_id)
    if not storeys:
        _fail_stage(
            repo,
            source_id,
            "No storey geometry to generate from; run floor segmentation first.",
        )
        raise VolumeGenerationFailure(
            f"Source {source_id} has no segmented storeys. Run "
            "POST /point-clouds/{id}/segment-floors first.",
            409,
        )

    parcels = repo.records("parcels")
    job_id = f"JOB-{uuid.uuid4().hex[:12]}"
    repo.add(
        "processing_jobs",
        {
            "id": job_id,
            "source_id": source_id,
            "job_type": ProcessingJobType.PROPERTY_VOLUME_GENERATION.value,
            "status": JobStatus.RUNNING.value,
            "crs": source_crs,
            "detail": "Generating property volumes",
            "started_at": now(),
            "created_by": created_by,
            "metadata": {
                "method": volumes_engine.GENERATION_METHOD,
                "source_crs": source_crs,
                "processing_crs": processing_crs,
                "display_crs": crs_service.WGS84,
            },
        },
    )

    stored: list[dict[str, Any]] = []
    per_building: list[dict[str, Any]] = []
    undivided: list[str] = []
    warnings: list[str] = []
    units_generated = 0

    # A building's identity anchor: its source plus the ordinal extraction gave it
    # *within that source*. The ordinal is read from the building's own id
    # (``XB-{job}-{ordinal}``) rather than re-derived from this list, because this
    # list grows every time the source is re-extracted. Indexing it here made the
    # ref depend on how many times the pipeline had been run, so the same physical
    # building was B01 on the first run and B03 on the third, and its ULPIN changed
    # with it -- the instability this was meant to remove, reintroduced.
    #
    # Taking the ordinal from the id gives what stability is actually available:
    # re-processing one source re-derives the same buildings in the same order, so
    # the ref is unchanged, ``_upsert_volume`` replaces instead of accumulating, and
    # the identifier survives. Two sources stay distinct, which is right.
    ordinals = {
        str(b["id"]): _extraction_ordinal(str(b["id"]), fallback)
        for fallback, b in enumerate(buildings, start=1)
    }

    for building in buildings:
        building_storeys = [
            s for s in storeys if s.get("building_id") == building.get("id")
        ]
        if not building_storeys:
            warnings.append(
                f"Building {building.get('id')} has no segmented storeys and was "
                "skipped"
            )
            continue

        generation = volumes_engine.generate_property_volumes(
            building,
            building_storeys,
            source_crs=processing_crs,
            parcels=parcels,
            floor_plans=floor_plans,
            ground_datum=ground_datum,
            building_ref=volumes_engine.stable_building_ref(
                source_id, ordinals.get(building.get("id"), 0)
            ),
            source_provenance={
                "source_id": source_id,
                "building_id": building.get("id"),
                "building_processing_job_id": building.get("processing_job_id"),
                "building_method": building.get("method"),
                "source_crs": source_crs,
                "processing_crs": processing_crs,
                "display_crs": crs_service.WGS84,
            },
        )
        warnings.extend(generation.warnings)
        undivided.extend(generation.floors_without_unit_data)

        for volume in generation.volumes:
            record = volume.to_record()
            record["source_id"] = source_id
            record["source_provenance"] = {
                **volume.source_provenance,
                "source_id": source_id,
                "building_id": building.get("id"),
                "generation_job_id": job_id,
            }
            stored_record = _upsert_volume(repo, record)
            stored.append(stored_record)
            # Two stages, because a generated volume arrives with an identifier
            # already attached. No model: the volume comes from a planar
            # subdivision of a surveyed footprint, or is left undivided.
            provenance.link_pipeline_output(
                repo,
                stage=ProvenanceStage.PROPERTY_VOLUME.value,
                object_id=stored_record["id"],
                source_id=source_id,
                processing_job_id=job_id,
                parent_id=(
                    stored_record.get("parent_floor_id")
                    or next(
                        (
                            s["id"]
                            for s in storeys
                            if s.get("building_id") == building.get("id")
                            and s.get("floor_number") == volume.floor_number
                        ),
                        job_id,
                    )
                ),
                parent_stage=ProvenanceStage.FLOOR.value,
                algorithm=volumes_engine.GENERATION_METHOD,
                method_description=volumes_engine.METHOD_DESCRIPTION,
                parameters={
                    "floor_number": volume.floor_number,
                    "volume_scope": volume.volume_scope,
                    "unit_label": volume.unit_label,
                    "units_inferred": False,
                    "building_id": building.get("id"),
                },
                created_by=created_by,
            )
            if stored_record.get("prototype_ulpin"):
                provenance.create_provenance_record(
                    repo,
                    stage=ProvenanceStage.ULPIN.value,
                    object_id=stored_record["prototype_ulpin"],
                    source_id=source_id,
                    processing_job_id=job_id,
                    parent_id=stored_record["id"],
                    parent_stage=ProvenanceStage.PROPERTY_VOLUME.value,
                    algorithm=volumes_engine.GENERATION_METHOD,
                    parameters={"volume_id": stored_record["id"]},
                    created_by=created_by,
                )
            if volume.volume_scope == "UNIT":
                units_generated += 1

        per_building.append(
            {
                "building_id": building.get("id"),
                "parent_parcel_id": generation.provenance.get("parent_parcel_id"),
                "parcel_match": generation.provenance.get("parcel_match"),
                "floors": generation.floors,
                "volumes": len(generation.volumes),
                "units_generated": generation.units_generated,
                "floors_without_unit_data": generation.floors_without_unit_data,
                "total_area_m2": generation.total_area_m2,
                "total_volume_m3": generation.total_volume_m3,
                "requires_human_review": generation.requires_human_review,
            }
        )

    detail = (
        f"Generated {len(stored)} property volume{'' if len(stored) == 1 else 's'} "
        f"from {len(per_building)} building{'' if len(per_building) == 1 else 's'}"
    )
    flagged = [v for v in stored if v.get("requires_human_review")]
    repo.update(
        "processing_jobs",
        job_id,
        {
            "status": JobStatus.COMPLETED.value,
            "detail": detail,
            "completed_at": now(),
            "metadata": {
                "method": volumes_engine.GENERATION_METHOD,
                "method_description": volumes_engine.METHOD_DESCRIPTION,
                "volumes_generated": len(stored),
                "units_generated": units_generated,
                "buildings": len(per_building),
                "floors_without_unit_data": undivided,
                #: Recorded so the job trail alone shows that no unit boundary
                #: was invented, without having to read the volumes.
                "units_inferred": False,
                "requires_human_review": bool(flagged),
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
            "stage": ProcessingJobType.PROPERTY_VOLUME_GENERATION.value,
            "processing_job_id": job_id,
            "volumes_generated": len(stored),
            "units_generated": units_generated,
            "buildings": len(per_building),
            "method": volumes_engine.GENERATION_METHOD,
            "units_inferred": False,
            "requires_human_review": bool(flagged),
        },
    )

    return VolumeGenerationResult(
        source_id=source_id,
        processing_job_id=job_id,
        method=volumes_engine.GENERATION_METHOD,
        method_description=volumes_engine.METHOD_DESCRIPTION,
        volumes_generated=len(stored),
        units_generated=units_generated,
        buildings=per_building,
        volumes=[GeneratedPropertyVolumeRecord(**_as_schema(v)) for v in stored],
        floors_without_unit_data=undivided,
        requires_human_review=bool(flagged),
        review_reasons=sorted({r for v in stored for r in (v.get("review_reasons") or [])}),
        warnings=warnings,
        label=ulpin_service.ULPIN_LABEL,
        note=DISCLAIMER,
    )


def list_generated_volumes(
    repo: CadastreRepository,
    *,
    source_id: str | None = None,
    building_id: str | None = None,
    parcel_id: str | None = None,
) -> list[dict[str, Any]]:
    """Generated volumes, optionally filtered by source, building or parcel."""
    records = repo.records("generated_property_volumes")
    if source_id:
        records = [r for r in records if r.get("source_id") == source_id]
    if building_id:
        records = [r for r in records if r.get("building_id") == building_id]
    if parcel_id:
        records = [r for r in records if r.get("parent_parcel_id") == parcel_id]
    return records


def _as_schema(record: dict[str, Any]) -> dict[str, Any]:
    allowed = set(GeneratedPropertyVolumeRecord.model_fields)
    return {k: v for k, v in record.items() if k in allowed}


def _upsert_volume(
    repo: CadastreRepository, record: dict[str, Any]
) -> dict[str, Any]:
    """Store a volume, replacing any earlier run's record for the same identity.

    The identifier is a function of stable attributes, so re-running generation
    for the same building and storey legitimately produces the **same**
    identifier -- that stability is the point, and ``prototype_ulpin`` is unique.
    A second run therefore replaces the first rather than accumulating a
    duplicate, and replaces it by delete-then-insert so a field that has become
    unset (a ``unit_label`` dropped when a plan is withdrawn) does not keep its
    stale value, which an update cannot express.
    """
    for existing in repo.records("generated_property_volumes"):
        if (
            existing.get("prototype_ulpin") == record["prototype_ulpin"]
            and existing.get("source_id") == record.get("source_id")
        ):
            repo.delete("generated_property_volumes", existing["id"])
            break
    return repo.add("generated_property_volumes", record)


def _fail_stage(repo: CadastreRepository, source_id: str, error: str) -> None:
    """Mark the outstanding generation stage failed, in its own transaction."""
    from app.db.session import session_scope, uses_postgres
    from app.repositories.postgres import PostgresCadastreRepository

    def apply(target: CadastreRepository) -> None:
        for job in target.records("processing_jobs"):
            if (
                job.get("source_id") == source_id
                and job.get("job_type")
                == ProcessingJobType.PROPERTY_VOLUME_GENERATION.value
                and job.get("status") == JobStatus.PENDING.value
            ):
                target.update(
                    "processing_jobs",
                    job["id"],
                    {
                        "status": JobStatus.FAILED.value,
                        "error": error,
                        "detail": "Property-volume generation failed",
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
    closed = 0
    for job in repo.records("processing_jobs"):
        if (
            job.get("source_id") == source_id
            and job.get("job_type")
            == ProcessingJobType.PROPERTY_VOLUME_GENERATION.value
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
    "VolumeGenerationFailure",
    "generate_volumes",
    "list_generated_volumes",
]
