"""Source-to-cadastral provenance.

Answers, for any object in the system: *where did this come from?*

    DATA_SOURCE -> PROCESSING_JOB -> BUILDING -> FLOOR
                -> PROPERTY_VOLUME -> ULPIN -> VALIDATION -> REVIEW

Stored as a graph of links rather than columns on each object, for two reasons.
A generated property volume has one source but several ancestors it also
contributes to -- a finding about it, a review of that finding -- and a single
"source_id" column cannot express that. And the later stages are events, not
geometry: a ULPIN issuance and a validation run have no footprint to hang a
foreign key off.

A **link** is the unit of storage: one row per step from a parent to a child,
carrying the metadata of the step.

On model information
--------------------
A provenance record has ``model_name`` and ``model_version`` fields, and
**they are empty for everything this system currently does.** Every stage here is
algorithmic or geometric: a progressive grid ground filter, DBSCAN, a
concavity hull, an elevation-histogram peak search, a planar subdivision. There
is no trained model anywhere in the pipeline, so a model name would be a
fabrication.

The fields exist because the chain has to accommodate one the day it does, and a
provenance record with no place for them would push the information somewhere
unqueryable. They are populated only by a caller that genuinely ran a model.
:data:`ALGORITHMIC_METHODS` lists the current methods, and
``test_provenance.py::test_no_model_information_is_fabricated`` asserts that none
of them carries a model name, and that nothing anywhere in the system does.

The same rule applies to ``algorithm``: it must be the real method identifier,
never a label chosen to look authoritative.

Honesty about gaps
------------------
A stage with no recorded link is **absent**, not inferred. An object with no
lineage is reported as having none. Filling a gap with a plausible parent would
make the provenance worse than useless: it would look complete and be wrong.
The demo's cadastral records are exactly this case -- they are seeded
synthetic geometry with no survey behind them, and
``get_object_lineage`` says so.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from app.models.enums import STAGE_ORDER, ProvenanceStage
from app.repositories.base import CadastreRepository
from app.utils import now

#: The methods this system actually runs. Every one is deterministic or
#: geometric, so none of them may carry a model name -- see the module docstring.
#: Default method label for a link that does not originate in the pipeline.
DEFAULT_METHOD = "registered-source"
#: The method identifiers this system actually records, taken from the
#: ``method`` / ``SEGMENTATION_METHOD`` / ``GENERATION_METHOD`` constants of the
#: pipeline rather than written out here. A descriptive name that looks more
#: authoritative than the real one would be exactly the invention this guard
#: exists to prevent, so this list holds the values the code emits.
ALGORITHMIC_METHODS: frozenset[str] = frozenset(
    {
        "algorithmic_geometric",
        "derived_geometric",
        DEFAULT_METHOD,
    }
)

#: Stage -> the repository collection its objects live in, where one exists.
#: ``ULPIN``, ``VALIDATION`` and ``REVIEW`` describe events and identifiers
#: rather than geometry-bearing collections, so they have no collection here.
STAGE_COLLECTION: dict[str, str | None] = {
    ProvenanceStage.DATA_SOURCE.value: "sources",
    ProvenanceStage.PROCESSING_JOB.value: "processing_jobs",
    ProvenanceStage.BUILDING.value: "extracted_buildings",
    ProvenanceStage.FLOOR.value: "extracted_floors",
    ProvenanceStage.PROPERTY_VOLUME.value: "generated_property_volumes",
    ProvenanceStage.ULPIN.value: None,
    ProvenanceStage.VALIDATION.value: "issues",
    ProvenanceStage.REVIEW.value: "review_cases",
}




class ProvenanceError(RuntimeError):
    """Raised when a link cannot be recorded as asked."""


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def create_provenance_record(
    repo: CadastreRepository,
    *,
    stage: str,
    object_id: str,
    source_id: str | None = None,
    processing_job_id: str | None = None,
    parent_id: str | None = None,
    parent_stage: str | None = None,
    algorithm: str | None = None,
    method_description: str | None = None,
    model_name: str | None = None,
    model_version: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    created_by: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Record one step of a lineage and return it.

    ``stage`` is where this link *lands*; ``parent_stage``/``parent_id`` are where
    it came from. A link with no parent is a root, which is legitimate: a hand
    uploaded GeoJSON genuinely has no parent.

    ``model_name``/``model_version`` are accepted so a caller that really did run
    a model can record it, and are rejected for the known algorithmic methods --
    see :data:`ALGORITHMIC_METHODS`. A placeholder is worse than a blank field.
    """
    valid = {member.value for member in ProvenanceStage}
    if stage not in valid:
        raise ProvenanceError(
            f"unknown provenance stage {stage!r}; expected one of {sorted(valid)}"
        )
    if parent_stage is not None and parent_stage not in valid:
        raise ProvenanceError(
            f"unknown parent stage {parent_stage!r}; expected one of {sorted(valid)}"
        )
    if not object_id:
        raise ProvenanceError("object_id is required to record provenance")
    if algorithm in ALGORITHMIC_METHODS and (model_name or model_version):
        # Refuse rather than store. A model name attached to a grid filter would
        # be read later as "a model produced this", which is false.
        raise ProvenanceError(
            f"{algorithm!r} is a deterministic algorithm and has no model; "
            "model_name and model_version must be left empty"
        )

    return repo.add(
        "provenance_links",
        {
            "id": f"PRV-{uuid.uuid4().hex[:12]}",
            "stage": stage,
            "object_id": str(object_id),
            "source_id": source_id,
            "processing_job_id": processing_job_id,
            "parent_id": parent_id,
            "parent_stage": parent_stage,
            "algorithm": algorithm or DEFAULT_METHOD,
            "method_description": method_description,
            "model_name": model_name,
            "model_version": model_version,
            "parameters": dict(parameters) if parameters else None,
            "created_by": created_by,
            "created_at": created_at or now(),
        },
    )


def link_source_to_object(
    repo: CadastreRepository,
    source_id: str,
    object_id: str,
    *,
    stage: str,
    algorithm: str | None = None,
    method_description: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Link a data source directly to a derived object.

    The short path, for a source that reaches an object with no processing step
    in between -- a hand-registered plan that a human turned into a volume
    directly, say.
    """
    return create_provenance_record(
        repo,
        stage=stage,
        object_id=object_id,
        source_id=source_id,
        parent_id=source_id,
        parent_stage=ProvenanceStage.DATA_SOURCE.value,
        algorithm=algorithm or DEFAULT_METHOD,
        method_description=method_description,
        parameters=parameters,
        created_by=created_by,
    )


def link_processing_job_to_object(
    repo: CadastreRepository,
    processing_job_id: str,
    object_id: str,
    *,
    stage: str,
    source_id: str | None = None,
    algorithm: str | None = None,
    method_description: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Link a processing job to the object it produced.

    The usual path in this system, where every derived object comes out of a
    recorded job. ``source_id`` is denormalised onto the link so a query can find
    everything from a source without walking the chain.
    """
    return create_provenance_record(
        repo,
        stage=stage,
        object_id=object_id,
        source_id=source_id,
        processing_job_id=processing_job_id,
        parent_id=processing_job_id,
        parent_stage=ProvenanceStage.PROCESSING_JOB.value,
        algorithm=algorithm or DEFAULT_METHOD,
        method_description=method_description,
        parameters=parameters,
        created_by=created_by,
    )


def record_job(
    repo: CadastreRepository,
    processing_job_id: str,
    source_id: str,
    *,
    job_type: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any] | None:
    """Link a processing job back to the data source it read.

    Idempotent: a job that produces twenty objects would otherwise collect twenty
    identical links. Returns the existing link when one is already recorded, and
    ``None`` when no ``source_id`` was supplied -- a job with no source has no
    origin, and inventing one is worse than recording none.
    """
    if not source_id:
        return None
    for link in repo.records("provenance_links"):
        if (
            link.get("stage") == ProvenanceStage.PROCESSING_JOB.value
            and link.get("object_id") == processing_job_id
        ):
            return link
    return create_provenance_record(
        repo,
        stage=ProvenanceStage.PROCESSING_JOB.value,
        object_id=processing_job_id,
        source_id=source_id,
        parent_id=source_id,
        parent_stage=ProvenanceStage.DATA_SOURCE.value,
        algorithm=job_type or "processing",
        parameters={"job_type": job_type} if job_type else None,
        created_by=created_by,
    )


def link_pipeline_output(
    repo: CadastreRepository,
    *,
    stage: str,
    object_id: str,
    source_id: str,
    processing_job_id: str,
    parent_id: str,
    parent_stage: str,
    algorithm: str,
    method_description: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Link a pipeline output to the job that produced it and to its predecessor.

    **Two links, not one.** A segmented storey has a processing job *and* an
    extracted building behind it, and a single ``parent_id`` cannot hold both.
    Recording one edge per real relationship keeps the chain walkable in both
    directions: from the volume you reach the storey, the building and the jobs
    and the source, which is what makes the full eight-stage lineage resolve.

    The job's own link back to the source is recorded here too, so the chain does
    not depend on a separate call every caller has to remember.
    """
    record_job(
        repo,
        processing_job_id,
        source_id,
        job_type=stage,
        created_by=created_by,
    )
    payload = dict(parameters or {})
    payload.setdefault("derived_from", parent_id)
    payload.setdefault("derived_from_stage", parent_stage)
    return create_provenance_record(
        repo,
        stage=stage,
        object_id=object_id,
        source_id=source_id,
        processing_job_id=processing_job_id,
        parent_id=parent_id,
        parent_stage=parent_stage,
        algorithm=algorithm,
        method_description=method_description,
        parameters=payload,
        created_by=created_by,
    )


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def _stage_rank(stage: str | None) -> int:
    """Position in the canonical order; unknown stages sort last."""
    try:
        return STAGE_ORDER.index(stage) if stage else len(STAGE_ORDER)
    except ValueError:
        return len(STAGE_ORDER)


def get_provenance(
    repo: CadastreRepository, object_id: str
) -> list[dict[str, Any]]:
    """Every provenance link mentioning ``object_id``, in stage order.

    Matches both roles: a link where the object is the result, and one where it
    is the parent. An object usually has both -- it was produced by a job and it
    then produced something else.
    """
    links = [
        link
        for link in repo.records("provenance_links")
        if link.get("object_id") == object_id or link.get("parent_id") == object_id
    ]
    return sorted(
        links, key=lambda l: (_stage_rank(l.get("stage")), str(l.get("created_at") or ""))
    )


def get_object_lineage(
    repo: CadastreRepository, object_id: str
) -> dict[str, Any]:
    """The full chain from a data source to an object, plus its descendants.

    Returns ``lineage`` as an ordered list of stages with the object reached at
    each, ``ancestors`` and ``descendants`` as plain id lists, and
    ``complete``. ``complete`` is ``False`` when the chain does not begin at a
    data source -- which is the honest answer for an object that was never
    derived from a survey, and is deliberately not filled in with a guess.
    """
    links = get_provenance(repo, object_id)
    if not links:
        return {
            "object_id": object_id,
            "lineage": [],
            "ancestors": [],
            "descendants": [],
            "source_id": None,
            "processing_job_ids": [],
            "complete": False,
            "note": (
                "No recorded provenance for this object. It may be hand-entered "
                "or predate provenance tracking; the absence is reported rather "
                "than inferred."
            ),
        }

    ancestors = _walk(repo, object_id, direction="up")
    descendants = _walk(repo, object_id, direction="down")

    # One entry per stage, keeping the nearest object reached at that stage, so
    # the lineage reads as a path rather than a list of rows. The object asked
    # about seeds its own stage: otherwise a volume's lineage lists the floor it
    # came from and the ULPIN that identifies it, but never itself.
    own_stage = _stage_for(repo, object_id)
    lineage: list[dict[str, Any]] = []
    if own_stage:
        lineage.append(
            {
                "stage": own_stage,
                "object_id": object_id,
                "link_id": None,
                "algorithm": None,
                "model_name": None,
                "model_version": None,
                "source_id": None,
                "processing_job_id": None,
                "created_at": None,
            }
        )
    seen: set[str] = {object_id}
    for stage in STAGE_ORDER:
        for node in ancestors:
            if node["stage"] == stage and node["object_id"] not in seen:
                seen.add(node["object_id"])
                lineage.append(node)
                break
        else:
            for node in descendants:
                if node["stage"] == stage and node["object_id"] not in seen:
                    seen.add(node["object_id"])
                    lineage.append(node)
                    break
    lineage.sort(key=lambda n: _stage_rank(n["stage"]))

    source_ids = {
        n["object_id"]
        for n in ancestors
        if n["stage"] == ProvenanceStage.DATA_SOURCE.value
    }
    # Jobs come from two places: graph-reachable ancestors, and the
    # ``processing_job_id`` column on the object's own links. A storey is a child
    # of a building, so its segmentation job is not a graph ancestor of it -- but
    # the job that produced it is exactly what a reader wants listed.
    job_ids = {
        n["object_id"]
        for n in ancestors
        if n["stage"] == ProvenanceStage.PROCESSING_JOB.value
    } | {
        str(link["processing_job_id"])
        for link in repo.records("provenance_links")
        if link.get("object_id") == object_id and link.get("processing_job_id")
    }
    starts_at_source = bool(source_ids) and lineage[0]["stage"] == ProvenanceStage.DATA_SOURCE.value
    return {
        "object_id": object_id,
        "lineage": lineage,
        "ancestors": [n["object_id"] for n in ancestors],
        "descendants": [n["object_id"] for n in descendants],
        "source_id": sorted(source_ids)[0] if source_ids else None,
        "processing_job_ids": sorted(job_ids),
        "complete": starts_at_source,
        "note": None
        if starts_at_source
        else (
            "The chain does not begin at a recorded data source. Intermediate "
            "stages are reported as they were recorded; the missing origin is "
            "not inferred."
        ),
    }


def _walk(
    repo: CadastreRepository, object_id: str, *, direction: str
) -> list[dict[str, Any]]:
    """Breadth-first walk of the provenance graph, one direction only.

    Walking **up** means following ``parent_id`` from links whose ``object_id`` is
    the current node -- an output is the *child* of what produced it. Walking
    **down** is the mirror: follow ``object_id`` from links whose ``parent_id`` is
    the current node.

    The stage of a node comes from ``parent_stage`` on the way up and from
    ``stage`` on the way down. Getting that backwards labels every ancestor with
    its child's stage, which turns a five-stage chain into five copies of the
    last one.

    Bounded and cycle-safe: a malformed chain cannot spin forever, and the visited
    set keeps a diamond (two paths to one object) from reporting it twice.
    """
    if direction == "up":
        match_field, step_field, step_stage = "object_id", "parent_id", "parent_stage"
    else:
        match_field, step_field, step_stage = "parent_id", "object_id", "stage"
    links = repo.records("provenance_links")
    out: list[dict[str, Any]] = []
    seen = {object_id}
    frontier = [object_id]
    while frontier:
        current = frontier.pop(0)
        for link in links:
            if link.get(match_field) != current:
                continue
            target = link.get(step_field)
            if not target or target in seen:
                continue
            seen.add(target)
            out.append(
                {
                    "stage": link.get(step_stage) or link.get("stage"),
                    "object_id": target,
                    "link_id": link.get("id"),
                    "algorithm": link.get("algorithm"),
                    "model_name": link.get("model_name"),
                    "model_version": link.get("model_version"),
                    "source_id": link.get("source_id"),
                    "processing_job_id": link.get("processing_job_id"),
                    "created_at": link.get("created_at"),
                }
            )
            frontier.append(target)
    out.sort(key=lambda n: _stage_rank(n["stage"]))
    return out

def _stage_for(repo: CadastreRepository, object_id: str) -> str | None:
    """The lineage stage an object was produced at, if it is recorded at all."""
    for link in repo.records("provenance_links"):
        if link.get("object_id") == object_id:
            return link.get("stage")
    return None

def get_derived_objects(
    repo: CadastreRepository, source_id: str, *, stage: str | None = None
) -> list[dict[str, Any]]:
    """Everything derived from one data source, grouped by stage.

    Answers "what did this upload actually produce?", which is the question a
    surveyor asks when deciding whether to keep or re-upload a file.
    """
    links = [
        link
        for link in repo.records("provenance_links")
        if link.get("source_id") == source_id
        and (stage is None or link.get("stage") == stage)
    ]
    links.sort(
        key=lambda l: (_stage_rank(l.get("stage")), str(l.get("created_at") or ""), str(l.get("id")))
    )
    by_stage: dict[str, list[str]] = {}
    for link in links:
        by_stage.setdefault(str(link.get("stage")), []).append(str(link.get("object_id")))
    return [
        {"stage": stage_name, "object_ids": sorted(set(ids)), "count": len(set(ids))}
        for stage_name, ids in sorted(
            by_stage.items(), key=lambda kv: _stage_rank(kv[0])
        )
    ]


def get_source_objects(
    repo: CadastreRepository, object_id: str
) -> list[str]:
    """The data sources behind an object, walking the whole chain.

    A list because more than one is possible: an object re-surveyed from a second
    upload has two origins, and reporting only the newest would hide the first.

    A source counts as behind the object if it is on a link the object produced,
    or on any link that object produced *transitively* -- a finding about a
    generated volume inherits the volume's origin, and that is the question
    ``get_source_objects`` is usually asked.
    """
    scope = {object_id} | _all_descendants(repo, object_id)
    sources = {
        str(link["source_id"])
        for link in repo.records("provenance_links")
        if link.get("object_id") in scope and link.get("source_id")
    }
    return sorted(sources)


def _all_descendants(repo: CadastreRepository, object_id: str) -> set[str]:
    links = repo.records("provenance_links")
    out: set[str] = set()
    frontier = [object_id]
    seen = {object_id}
    while frontier:
        current = frontier.pop(0)
        for link in links:
            if link.get("parent_id") == current and link.get("object_id") not in seen:
                seen.add(link["object_id"])
                out.add(link["object_id"])
                frontier.append(link["object_id"])
    return out


def get_processing_history(
    repo: CadastreRepository, object_id: str
) -> list[dict[str, Any]]:
    """The jobs that produced an object, oldest first.

    Read from the ``processing_jobs`` collection rather than from the links, so
    the job's recorded status, detail and method are the ones shown -- a link is
    a claim, the job row is the record.
    """
    job_ids: set[str] = set()
    for link in repo.records("provenance_links"):
        if link.get("object_id") == object_id and link.get("processing_job_id"):
            job_ids.add(str(link["processing_job_id"]))
    # A source that fed a job is not itself a job, but a job reached through the
    # lineage is.
    for ancestor in get_object_lineage(repo, object_id)["ancestors"]:
        if ancestor in {
            str(j.get("id")) for j in repo.records("processing_jobs")
        }:
            job_ids.add(ancestor)

    jobs = {str(j.get("id")): j for j in repo.records("processing_jobs")}
    history = [
        {
            "processing_job_id": job_id,
            "job_type": jobs[job_id].get("job_type") if job_id in jobs else None,
            "status": jobs[job_id].get("status") if job_id in jobs else None,
            "detail": jobs[job_id].get("detail") if job_id in jobs else None,
            "source_id": jobs[job_id].get("source_id") if job_id in jobs else None,
            "started_at": jobs[job_id].get("started_at") if job_id in jobs else None,
            "completed_at": jobs[job_id].get("completed_at") if job_id in jobs else None,
        }
        for job_id in job_ids
    ]
    return sorted(history, key=lambda j: str(j.get("started_at") or j["processing_job_id"]))


__all__ = [
    "ALGORITHMIC_METHODS",
    "DEFAULT_METHOD",
    "STAGE_COLLECTION",
    "ProvenanceError",
    "create_provenance_record",
    "get_derived_objects",
    "get_object_lineage",
    "get_processing_history",
    "get_provenance",
    "get_source_objects",
    "link_pipeline_output",
    "record_job",
    "link_processing_job_to_object",
    "link_source_to_object",
]
