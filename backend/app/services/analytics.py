"""Cadastral analytics for the command-centre snapshot panel.

Counts are derived from the active store. The per-level ``distribution`` is
hardcoded to the demo scene's known shape and does not reflect arbitrary data --
preserved from the original implementation, and called out here because it is a
known limitation rather than a measurement.

``average_confidence`` was removed. It averaged a ``confidence`` field that every
seeded record carried as one of two literals, so the figure was a constant
dressed as a statistic. Nothing here reports an accuracy.
"""
from __future__ import annotations

from typing import Any

from app.models.enums import IssueSeverity, PropertyType
from app.repositories.base import CadastreRepository
from app.services.demo import FLOOR_COUNT


def _distribution() -> list[dict[str, Any]]:
    """Prototype level breakdown: utility, basement, then floors top-down.

    Fixed to the demo scene's shape. It does not read the store, so it is wrong
    for any other data -- a pre-existing limitation, kept so the panel renders.
    """
    return (
        [{"level": "Utility", "count": 1}, {"level": "Basement", "count": 1}]
        + [{"level": f"Floor {n}", "count": 2} for n in range(FLOOR_COUNT, 0, -1)]
    )


def summarise(repo: CadastreRepository) -> dict[str, Any]:
    """Aggregate counts and conflicts for the scene.

    Every figure below is a count of records in the store. No accuracy, score or
    confidence is reported, because none is measured.
    """
    issues = repo.records("issues")
    props = repo.records("properties")
    return {
        "parcels": len(repo.records("parcels")),
        "buildings": len(repo.records("buildings")),
        "property_volumes": len(props),
        "apartments": sum(p["property_type"] == PropertyType.APARTMENT.value for p in props),
        "underground_assets": len(repo.records("infrastructure")),
        "validation_issues": len(issues),
        "critical_conflicts": sum(
            x["severity"] == IssueSeverity.CRITICAL for x in issues
        ),
        "distribution": _distribution(),
    }


__all__ = ["summarise"]
