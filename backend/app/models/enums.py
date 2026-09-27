"""Domain vocabulary for the cadastral model.

``Severity`` stays a ``Literal`` alias rather than an ``Enum`` because it is
used directly as a Pydantic field type on ``ValidationIssue``; keeping it a
``Literal`` guarantees identical request/response serialisation.

The ``str`` enums below are used for comparisons and constants. Records store
plain ``str`` values (``.value``) so that serialised output is byte-identical
to the original implementation.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

#: Validation severity. ``INFO`` is part of the contract but is never emitted
#: by the current rule set.
Severity = Literal["INFO", "WARNING", "CRITICAL"]


class PropertyType(str, Enum):
    APARTMENT = "APARTMENT"
    BASEMENT_PARKING = "BASEMENT_PARKING"
    #: Generated volumes. ``UNIT`` is used only where floor-plan data actually
    #: divided a storey; ``FLOOR`` is an undivided storey plate, which is a
    #: statement about extent and not a claim of single ownership.
    UNIT = "UNIT"
    FLOOR = "FLOOR"
    BASEMENT = "BASEMENT"


class PropertyStatus(str, Enum):
    MAPPED = "MAPPED"
    HUMAN_REVIEW_REQUIRED = "HUMAN REVIEW REQUIRED"


class InfrastructureType(str, Enum):
    UNDERGROUND_UTILITY_CORRIDOR = "UNDERGROUND_UTILITY_CORRIDOR"


class RuleCategory(str, Enum):
    """Grouping for a validation rule. Determines nothing but presentation."""

    GEOMETRY = "GEOMETRY"
    PARCEL = "PARCEL"
    VERTICAL = "VERTICAL"
    CADASTRAL = "CADASTRAL"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    CHANGE = "CHANGE"


class IssueType(str, Enum):
    INVALID_GEOMETRY = "INVALID_GEOMETRY"
    OUTSIDE_PARENT_PARCEL = "OUTSIDE_PARENT_PARCEL"
    VERTICAL_VOLUME_OVERLAP = "VERTICAL_VOLUME_OVERLAP"
    FLOOR_Z_ORDER_INCONSISTENCY = "FLOOR_Z_ORDER_INCONSISTENCY"
    EXPECTED_ADJACENT_FLOOR_GAP = "EXPECTED_ADJACENT_FLOOR_GAP"
    DISCONNECTED_FLOOR_STACK = "DISCONNECTED_FLOOR_STACK"
    DUPLICATE_ULPIN_GEOMETRY = "DUPLICATE_ULPIN_GEOMETRY"
    UNDERGROUND_INFRASTRUCTURE_COLLISION = "UNDERGROUND_INFRASTRUCTURE_COLLISION"
    # Added by the rule-based engine. The eight above are unchanged: their
    # values are part of the stored-record contract.
    SELF_INTERSECTION = "SELF_INTERSECTION"
    ZERO_AREA = "ZERO_AREA"
    ZERO_VOLUME = "ZERO_VOLUME"
    INVALID_Z_RANGE = "INVALID_Z_RANGE"
    BUILDING_OUTSIDE_PARCEL = "BUILDING_OUTSIDE_PARCEL"
    UNASSIGNED_BUILDING = "UNASSIGNED_BUILDING"
    UNASSIGNED_PROPERTY = "UNASSIGNED_PROPERTY"
    FLOOR_OVERLAP = "FLOOR_OVERLAP"
    INCONSISTENT_FLOOR_HEIGHT = "INCONSISTENT_FLOOR_HEIGHT"
    DUPLICATE_GEOMETRY = "DUPLICATE_GEOMETRY"
    MISSING_PARENT = "MISSING_PARENT"
    ORPHAN_PROPERTY = "ORPHAN_PROPERTY"
    INVALID_HIERARCHY = "INVALID_HIERARCHY"
    TUNNEL_COLLISION = "TUNNEL_COLLISION"
    BASEMENT_COLLISION = "BASEMENT_COLLISION"
    FOUNDATION_COLLISION = "FOUNDATION_COLLISION"
    ELEVATED_STRUCTURE_COLLISION = "ELEVATED_STRUCTURE_COLLISION"
    FOOTPRINT_CHANGE = "FOOTPRINT_CHANGE"
    HEIGHT_CHANGE = "HEIGHT_CHANGE"
    VOLUME_CHANGE = "VOLUME_CHANGE"
    NEW_FLOOR = "NEW_FLOOR"
    REMOVED_FLOOR = "REMOVED_FLOOR"
    #: A storey present in a new survey that is absent from the approved
    #: cadastre. Emitted by change detection, which needs a baseline the rule
    #: engine does not otherwise have -- see ``services/change_detection.py``.
    UNREGISTERED_FLOOR = "UNREGISTERED_FLOOR"


class IssueSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class SourceType(str, Enum):
    SYNTHETIC_GEOJSON = "Synthetic GeoJSON"
    GEOJSON = "GeoJSON"
    FLOOR_PLAN_JSON = "Floor-plan JSON"
    CSV = "CSV"
    POINT_CLOUD = "Point cloud"
    UNSPECIFIED = "Unspecified"


class PointCloudFormat(str, Enum):
    """Supported point-cloud container formats."""

    LAS = "LAS"
    LAZ = "LAZ"
    PLY = "PLY"


class VolumeScope(str, Enum):
    """What a generated volume actually represents.

    The distinction is the point of the milestone: a storey plate with no
    floor-plan data is a ``FLOOR`` volume, which is a statement about geometry
    and **not** a claim that the storey is one ownership unit.
    """

    UNIT = "UNIT"
    FLOOR = "FLOOR"


class JobStatus(str, Enum):
    """Lifecycle of a processing job.

    ``COMPLETED`` currently means "metadata extracted". The extraction stages
    (building footprint, floor segmentation) are not implemented yet, so no job
    ever claims to have produced features.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class ProcessingJobType(str, Enum):
    """What a job was asked to do."""

    METADATA_EXTRACTION = "METADATA_EXTRACTION"
    BUILDING_EXTRACTION = "BUILDING_EXTRACTION"
    FLOOR_SEGMENTATION = "FLOOR_SEGMENTATION"
    VERTICAL_DELINEATION = "VERTICAL_DELINEATION"
    PROPERTY_VOLUME_GENERATION = "PROPERTY_VOLUME_GENERATION"


class ReviewState(str, Enum):
    """Where a finding sits in the human-verification workflow.

    The flow is ``ISSUE -> REVIEW -> decision``::

        (no case)      the finding is OPEN, nobody has looked at it
        PENDING        a case exists, no reviewer assigned
        IN_REVIEW      assigned to a reviewer
        APPROVED       a human confirmed the finding is real and correct
        REJECTED       a human dismissed it as a false positive
        RESURVEY_REQUESTED
                       the finding is real but the geometry is wrong, so the
                       object must be re-surveyed rather than the finding closed
        EXPECTED       the condition is a known, accepted characteristic -- an
                       overhang or a shared service duct, say -- and is not a
                       defect to fix
        CLOSED         the finding is finished with
        OPEN           reopened after closure, back to awaiting review

    ``RESURVEY_REQUESTED`` is deliberately distinct from ``REJECTED``: one says
    the *finding* is wrong, the other says the *geometry* is wrong. Collapsing
    them would lose the only distinction that matters to whoever has to re-measure.
    """

    OPEN = "OPEN"
    PENDING = "PENDING"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RESURVEY_REQUESTED = "RESURVEY_REQUESTED"
    EXPECTED = "EXPECTED"
    CLOSED = "CLOSED"


#: States a review case may still be decided from. A case is decided exactly
#: once; to revisit a decision, close or reopen the issue instead.
DECIDABLE_STATES: frozenset[str] = frozenset(
    {ReviewState.PENDING.value, ReviewState.IN_REVIEW.value}
)


class AuditAction(str, Enum):
    """What happened, in the vocabulary of cadastral governance.

    The first ten are the operations an auditor must be able to reconstruct.
    The rest exist because the review workflow moves state in ways that would
    otherwise have to be recorded as a vague ``UPDATED``, which is exactly the
    kind of imprecision an audit trail cannot afford.
    """

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    IMPORTED = "IMPORTED"
    PROCESSED = "PROCESSED"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RESURVEY_REQUESTED = "RESURVEY_REQUESTED"
    GEOMETRY_CHANGED = "GEOMETRY_CHANGED"
    ULPIN_GENERATED = "ULPIN_GENERATED"
    # Review-workflow state transitions that have no honest ``UPDATED`` equivalent.
    REVIEW_REQUESTED = "REVIEW_REQUESTED"
    REVIEW_ASSIGNED = "REVIEW_ASSIGNED"
    MARKED_EXPECTED = "MARKED_EXPECTED"
    ISSUE_CLOSED = "ISSUE_CLOSED"
    ISSUE_REOPENED = "ISSUE_REOPENED"
    #: A survey disagrees with the approved cadastre. Records *that* a
    #: difference exists and nothing more -- never that anyone was at fault.
    CHANGE_DETECTED = "CHANGE_DETECTED"


class ChangeType(str, Enum):
    """What kind of difference a new survey found against the approved cadastre.

    These name a *measured difference*, not a cause and not a fault. A
    ``FOOTPRINT_CHANGE`` is a bigger polygon; it is not, by itself, an
    encroachment, a trespass or an unauthorised alteration. See
    :data:`CHANGE_REQUIRES_VERIFICATION`.
    """

    FOOTPRINT = "FOOTPRINT"
    HEIGHT = "HEIGHT"
    VOLUME = "VOLUME"
    NEW_FLOOR = "NEW_FLOOR"
    REMOVED_FLOOR = "REMOVED_FLOOR"


class ChangeStatus(str, Enum):
    """Where a detected change sits in the verification workflow.

    There is deliberately no "illegal", "unauthorised" or "violation" value. A
    geometric difference is a fact about two measurements; whether anyone was
    entitled to make it is a question for a human, and answering it in code
    would be the system asserting something it cannot know.
    """

    #: Detected and waiting for a human to look at it.
    REQUIRES_VERIFICATION = "REQUIRES_VERIFICATION"
    #: A human has looked and agreed the change should stand.
    VERIFIED = "VERIFIED"
    #: A human dismissed the difference as measurement noise or an artefact of
    #: survey extent. The geometry is left alone.
    DISMISSED = "DISMISSED"


#: The single sentence the system uses whenever it reports a difference.
#:
#: Wording matters here. Every alternative considered ("unauthorised change",
#: "illegal alteration", "encroachment detected") asserts a fact the comparison
#: cannot establish: it saw two geometries that differ, and nothing about who
#: changed what or whether they were entitled to.
CHANGE_REQUIRES_VERIFICATION = "Change detected — requires verification."


#: Actions that record a human decision. Their ``detail`` always carries the
#: reviewer's reason, which is the part an auditor actually needs.
DECISION_ACTIONS: frozenset[str] = frozenset(
    {
        AuditAction.APPROVED.value,
        AuditAction.REJECTED.value,
        AuditAction.RESURVEY_REQUESTED.value,
        AuditAction.MARKED_EXPECTED.value,
    }
)


class ProvenanceStage(str, Enum):
    """A stage in the chain from an ingested dataset to a reviewed finding.

    The order below is the canonical lineage, from where data came in to who
    looked at the result::

        DATA_SOURCE -> PROCESSING_JOB -> BUILDING -> FLOOR
                    -> PROPERTY_VOLUME -> ULPIN -> VALIDATION -> REVIEW

    Not every object traverses every stage. A hand-registered GeoJSON source has
    no processing job, and the demo's cadastral records were never derived from a
    survey at all -- claiming otherwise would fabricate a lineage.
    """

    DATA_SOURCE = "DATA_SOURCE"
    PROCESSING_JOB = "PROCESSING_JOB"
    BUILDING = "BUILDING"
    FLOOR = "FLOOR"
    PROPERTY_VOLUME = "PROPERTY_VOLUME"
    ULPIN = "ULPIN"
    VALIDATION = "VALIDATION"
    REVIEW = "REVIEW"


#: Presentation order for a lineage. Stages are always shown in this order, never
#: in insertion order, so two runs of the same pipeline read the same way.
STAGE_ORDER: tuple[str, ...] = tuple(member.value for member in ProvenanceStage)
