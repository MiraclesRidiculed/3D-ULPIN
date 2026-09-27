"""Validation entry point.

The rule logic now lives in :mod:`app.services.validation_engine`, which declares
thirty independent rules across six categories. This module is the adapter that
keeps the original API: :func:`validate` recomputes every finding from scratch and
replaces the stored set, returning the same summary shape it always has.

Behaviour is unchanged, deliberately down to the two crashes
--------------------------------------------------------
The original implementation appended an ``INVALID_GEOMETRY`` finding and then
continued into the containment check on the same invalid geometry, which makes
GEOS raise. It also serialised a ``LineString`` intersection for two footprints
that merely share an edge, which raises ``AttributeError``. Both are pinned by
``tests/test_services.py`` as pre-existing, and the engine's rule order reproduces
them. They are bugs, not design, but fixing them changes observable behaviour and
is therefore a separate, deliberate change.
"""
from __future__ import annotations

from typing import Any

from app.models.enums import AuditAction, IssueSeverity, ProvenanceStage
from app.repositories.base import CadastreRepository
from app.services.audit import create_audit_event
from app.services.reviews import restore_review_outcomes
from app.services.validation_engine import (
    AREA_EPSILON,
    GAP_EPSILON,
    SAME_FLOOR_ONLY,
    ValidationContext,
    ValidationEngine,
    ValidationResult,
    ValidationRule,
    get_engine,
    get_rule_definitions,
    get_validation_summary,
    persist_results,
    run_all_rules,
    run_rule,
)

#: Demo identifiers referenced in finding text. Hardcoded, as in the original.
DEMO_PARCEL_BUSINESS_ID = "P-001"
DEMO_BUILDING_BUSINESS_ID = "B-001"


def validate(repo: CadastreRepository) -> dict[str, Any]:
    """Run the full rule set and replace the stored findings.

    Recomputes from scratch on every call, so it is idempotent.

    A human's review outcome survives the rebuild: the issues collection is
    cleared and regenerated here, which would otherwise reset every
    ``status`` to ``OPEN`` and silently discard an approval. Reviewed states are
    carried forward onto the findings that still exist.

    .. warning::
       The rules that need a parent parcel and a parent structure index
       ``parcels[0]`` and ``infrastructure[0]``, and reference the demo parcel
       id ``P-001`` in finding text. On an empty store this raises
       ``IndexError``, as it always has.
    """
    repo.clear("issues")

    def record(result: ValidationResult) -> None:
        repo.add("issues", result.to_issue_record())

    # Findings are stored as they are produced rather than batched at the end, so
    # a rule that raises leaves the findings before it already recorded. That is
    # the original behaviour, and `test_invalid_geometry_is_appended_then_raises`
    # depends on it.
    run_all_rules(repo, on_result=record)
    restored = restore_review_outcomes(repo)
    issues = repo.records("issues")
    _record_validation_provenance(repo, issues)

    create_audit_event(
        repo,
        action=AuditAction.VALIDATED.value,
        object_id=None,
        object_type="SCENE",
        detail=(
            f"Validation run over {len(issues)} finding(s): "
            f"{sum(x['severity'] == IssueSeverity.CRITICAL for x in issues)} critical, "
            f"{sum(x['severity'] == IssueSeverity.WARNING for x in issues)} warning"
        ),
        extra={
            "findings": len(issues),
            "critical": sum(x["severity"] == IssueSeverity.CRITICAL for x in issues),
            "warning": sum(x["severity"] == IssueSeverity.WARNING for x in issues),
            "review_outcomes_restored": sorted(restored),
        },
    )
    return {
        "issues": len(issues),
        "critical": sum(x["severity"] == IssueSeverity.CRITICAL for x in issues),
        "warning": sum(x["severity"] == IssueSeverity.WARNING for x in issues),
    }


def _record_validation_provenance(
    repo: CadastreRepository, issues: list[dict[str, Any]]
) -> None:
    """Link each finding to the object it is about, in the VALIDATION stage.

    A finding about a derived object inherits that object's origin, so
    ``get_source_objects(finding_id)`` answers "which survey produced this
    problem?" without a second walk.

    A finding about a cadastral record with no provenance -- the demo scene, or
    anything hand-entered -- gets a link with **no** ``source_id``. That absence
    is the honest answer: the record was not derived from a survey, and inventing
    an origin would make the lineage look complete while being wrong.
    """
    from app.services import provenance

    for issue in issues:
        for object_id in (issue.get("object_a"), issue.get("object_b")):
            if not object_id:
                continue
            source_id = next(
                iter(provenance.get_source_objects(repo, object_id)), None
            )
            provenance.create_provenance_record(
                repo,
                stage=ProvenanceStage.VALIDATION.value,
                object_id=issue["id"],
                source_id=source_id,
                parent_id=object_id,
                parent_stage=(
                    _stage_for(repo, object_id)
                    or ProvenanceStage.PROPERTY_VOLUME.value
                ),
                algorithm=issue.get("rule_id") or "validation",
                parameters={
                    "issue_type": issue.get("issue_type"),
                    "severity": issue.get("severity"),
                    "about_object": object_id,
                },
            )


def _stage_for(repo: CadastreRepository, object_id: str) -> str | None:
    """Which lineage stage an object sits at, if it is recorded at all."""
    for link in repo.records("provenance_links"):
        if link.get("object_id") == object_id:
            return link.get("stage")
    return None


def validate_with_results(
    repo: CadastreRepository,
) -> list[ValidationResult]:
    """Run every rule and return the findings *without* persisting them.

    For callers that want the evidence and want to decide what to store.
    """
    return run_all_rules(repo)


__all__ = [
    "AREA_EPSILON",
    "GAP_EPSILON",
    "SAME_FLOOR_ONLY",
    "ValidationContext",
    "ValidationEngine",
    "ValidationResult",
    "ValidationRule",
    "get_engine",
    "get_rule_definitions",
    "get_validation_summary",
    "persist_results",
    "run_all_rules",
    "run_rule",
    "validate",
    "validate_with_results",
]
