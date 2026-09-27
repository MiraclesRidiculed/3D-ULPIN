"""Processing adapter endpoints.

These stand in for building extraction, floor segmentation and vertical
delineation. **No model is loaded and no inference is performed.**

The audit removed the accuracy figures these used to return. They were literals
(0.974, 0.918, 0.991) and a ``model`` key naming a "deterministic footprint
inference adapter" -- a model that does not exist. A number nobody measured,
labelled ``confidence``, is exactly the fabrication this project exists to avoid,
and it survived the earlier honesty pass because that pass only cleaned the
frontend.

What remains is a count read from the active store, and an explicit statement that
the figure is a store count rather than a result. The real, geometry-derived
measurements live in the actual pipeline: ``geometric_quality`` on an extracted
building, and the elevation-histogram evidence behind a detected storey level.
"""
from __future__ import annotations

from typing import Any

from app.repositories.base import CadastreRepository

#: Attached to every adapter response so a consumer cannot mistake a store count
#: for a measurement of the thing being counted.
ADAPTER_NOTE = (
    "Store count only. No model was loaded, no inference was performed, and no "
    "accuracy figure is reported: the numbers below are read from the active "
    "store, not measured from data. For a geometry-derived regularity score see "
    "geometric_quality on an extracted building."
)


def building_extraction(repo: CadastreRepository) -> dict[str, Any]:
    """Adapter: footprint extraction from imagery."""
    return {
        "stage": "building-extraction",
        "building_count": len(repo.records("buildings")),
        "adapter": True,
        "note": ADAPTER_NOTE,
    }


def floor_segmentation(repo: CadastreRepository) -> dict[str, Any]:
    """Adapter: storey detection within an extracted building."""
    return {
        "stage": "floor-segmentation",
        "storey_count": len(repo.records("floors")),
        "adapter": True,
        "note": ADAPTER_NOTE,
    }


def vertical_delineation(repo: CadastreRepository) -> dict[str, Any]:
    """Adapter: 3D right-boundary delineation across floors."""
    return {
        "stage": "vertical-delineation",
        "volume_count": len(repo.records("properties")),
        "adapter": True,
        "note": ADAPTER_NOTE,
    }
