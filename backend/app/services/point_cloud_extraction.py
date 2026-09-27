"""Building extraction from point-cloud data.

**This is algorithmic / geometric extraction, not machine learning.** No model
is trained, loaded or applied, and nothing produced here is a learned accuracy
figure. Every number is a measured geometric property of the input points.

Why algorithmic
---------------
A learned extractor needs a trained model and a labelled corpus, and this
prototype has neither. A geometric pipeline is deterministic, inspectable and
honest about what it does: it separates ground from structure, clusters the
structure, and turns each cluster into a footprint and a height. Its failure
modes are *visible* (low planarity, low density, ragged footprint) rather than
opaque.

Pipeline
--------
1. :func:`classify_ground` -- grid-based progressive filter. Ground is modelled
   per grid cell from a low percentile, then re-estimated from ground points
   only, with a shrinking threshold, until stable. Handles slope because the
   ground surface is local rather than global.
2. :func:`extract_non_ground_points` -- points a real distance above that local
   ground.
3. :func:`cluster_building_points` -- DBSCAN over XY, implemented with a spatial
   hash. Density-adaptive, so a dense facade and a sparse roof edge stay in one
   cluster without merging two genuinely separate buildings.
4. :func:`generate_building_footprints` -- concave hull of the projected cluster,
   falling back to the convex hull for sparse or convex clusters.
5. :func:`simplify_building_footprint` -- Douglas-Peucker simplification and a
   validity repair.
6. :func:`estimate_building_height` -- a high percentile of height above ground,
   which rejects vegetation spikes and noise that a maximum would not.
7. :func:`create_building_from_point_cloud` -- assembles one building, including
   the CRS-correct geometry hash.

Quality, not accuracy
---------------------
:func:`measure_building_quality` returns ``geometric_quality`` in [0, 1] plus
its raw components. It summarises geometric *regularity* -- roof flatness, point
density, footprint compactness, height stability. **It is not a probability of
correctness and not an accuracy score.** The components are returned
unnormalised so a reader can judge them directly.

No Shapely is imported here: every hull, simplification and measure is delegated
to :mod:`app.services.geometry`, the geometry engine.
"""
from __future__ import annotations

import math
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from app.services import crs as crs_service
from app.services import geometry as geometry_service
from app.utils import now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from shapely.geometry import Polygon

#: Bumped when the algorithm changes in a way that alters output.
EXTRACTOR_VERSION = "1.0.0"

#: Recorded on every result, so a consumer can never mistake this for an ML output.
EXTRACTION_METHOD = "algorithmic_geometric"

#: Plain-language statement of what the method is. Carried in every result.
METHOD_DESCRIPTION = (
    "Algorithmic / geometric extraction (grid ground filter, DBSCAN clustering, "
    "concave-hull footprints). Not machine learning; no model was trained or "
    "applied, and no accuracy figure is claimed."
)

# --------------------------------------------------------------------------
# Tunables. Distances in metres, in the point cloud's own projected CRS.
# --------------------------------------------------------------------------
DEFAULT_GROUND_CELL_SIZE = 1.0
DEFAULT_GROUND_PERCENTILE = 2.0
DEFAULT_GROUND_INITIAL_THRESHOLD = 0.60
DEFAULT_GROUND_MIN_THRESHOLD = 0.15
DEFAULT_GROUND_MAX_ITERATIONS = 5
DEFAULT_MIN_HEIGHT_ABOVE_GROUND = 0.50
#: A cell whose ground estimate differs from its neighbours' median by more than
#: this is treated as having no ground return (a cell wholly under a roof), and
#: adopts the neighbourhood value instead.
DEFAULT_GROUND_NEIGHBOUR_TOLERANCE = 1.00
DEFAULT_GROUND_NEIGHBOUR_PASSES = 2

DEFAULT_CLUSTER_EPS = 1.50
DEFAULT_CLUSTER_MIN_SAMPLES = 6
#: Points a cluster needs before it is treated as a structure.
#:
#: Set from measurement, not taste. A density scan of a building splits into a
#: dominant mass plus scattered sub-clusters along sparsely-scanned walls: on the
#: synthetic scenes those fragments run 5-45 points and 0.6-22 m², while genuine
#: buildings run 1,700-2,500 points. A threshold of 100 separates the two cleanly
#: with a wide margin on both sides. Lowering it lets wall fragments through as
#: phantom buildings sitting inside a real one.
DEFAULT_MIN_CLUSTER_POINTS = 100
#: Plan area a footprint needs. 20 m² is below the smallest structure worth
#: treating as a building, and above every wall fragment measured above.
DEFAULT_MIN_FOOTPRINT_AREA = 20.0

DEFAULT_CONCAVE_RATIO = 0.30
DEFAULT_SIMPLIFY_TOLERANCE = 0.25
DEFAULT_HEIGHT_PERCENTILE = 95.0

#: Guard rail so a pathological file cannot exhaust memory.
DEFAULT_MAX_POINTS = 2_000_000
#: Fraction of a cluster's points used for the roof plane fit.
ROOF_SAMPLE_FRACTION = 0.10
#: Minimum points for a reliable plane fit.
MIN_ROOF_PLANE_POINTS = 8


# --------------------------------------------------------------------------
# Data carriers
# --------------------------------------------------------------------------


@dataclass
class GroundModel:
    """Per-cell ground elevation over the point set.

    Stores the dense cell arrays rather than a dict so the lookup is a vector
    gather. :attr:`elevations` materialises the dict form on demand, for
    provenance and for callers that want to inspect cells.
    """

    cell_size: float
    #: ``(n_cells, 2)`` unique integer cell keys.
    cell_keys: np.ndarray
    #: ``(n_cells,)`` ground elevation per cell.
    cell_elevations: np.ndarray
    #: ``(n_points,)`` dense cell index per point, for vectorised lookup.
    cell_index: np.ndarray
    iterations: int = 0
    ground_point_count: int = 0
    final_threshold: float = DEFAULT_GROUND_MIN_THRESHOLD
    #: Cells that held no ground return and whose elevation was extrapolated
    #: from measured neighbours. Non-zero under buildings, which is expected.
    cells_without_ground: int = 0

    def height_above_ground(self) -> np.ndarray:
        """Height of every input point above the modelled local ground."""
        return self.cell_elevations[self.cell_index]

    def elevation_at(self, x: float, y: float) -> float:
        """Ground elevation at an arbitrary position.

        Averages the surrounding cells so a lookup on a cell boundary does not
        jump, and falls back to the median elevation when the position lies
        outside the modelled extent. Used to establish a building's base datum,
        which is what floor levels are measured from.
        """
        if self.cell_keys.shape[0] == 0:
            return 0.0
        target = np.array(
            [[math.floor(x / self.cell_size), math.floor(y / self.cell_size)]],
            dtype=np.int64,
        )
        offsets = [
            (dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)
        ]
        found: list[float] = []
        for dx, dy in [(0, 0), *offsets]:
            probe = target + np.array([[dx, dy]], dtype=np.int64)
            hit = np.flatnonzero((self.cell_keys == probe).all(axis=1))
            if hit.size:
                found.append(float(self.cell_elevations[hit[0]]))
        if not found:
            return float(np.median(self.cell_elevations))
        return float(np.mean(found))

    @property
    def elevations(self) -> dict[tuple[int, int], float]:
        return {
            (int(k[0]), int(k[1])): float(v)
            for k, v in zip(self.cell_keys, self.cell_elevations)
        }

    def summary(self) -> dict[str, Any]:
        return {
            "cell_size_m": self.cell_size,
            "cells": int(self.cell_keys.shape[0]),
            "iterations": self.iterations,
            "ground_points": self.ground_point_count,
            "final_threshold_m": self.final_threshold,
            "cells_without_ground_return": self.cells_without_ground,
        }


@dataclass
class PointCluster:
    """A group of non-ground points treated as one structure."""

    index: int
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    height_above_ground: np.ndarray

    @property
    def count(self) -> int:
        return int(self.x.size)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            float(self.x.min()),
            float(self.y.min()),
            float(self.x.max()),
            float(self.y.max()),
        )


@dataclass
class PointSet:
    """Points prepared for extraction, in the selected metric processing CRS."""

    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    crs: str
    format: str
    source_id: str
    declared_point_count: int
    read_in_chunks: bool = True
    truncated: bool = False
    #: Retained separately because ``crs`` describes the transformed arrays.
    source_crs: str | None = None
    processing_crs: str | None = None
    display_crs: str = crs_service.WGS84


@dataclass
class ExtractedBuilding:
    """One building produced by extraction.

    ``geometric_quality`` is a regularity score, **not** an accuracy figure.
    """

    footprint: "Polygon"
    height_m: float
    source_id: str
    processing_job_id: str
    method: str
    extractor: str
    extractor_version: str
    geometry_hash: str
    crs: str
    geometric_quality: float
    quality_metrics: dict[str, Any]
    provenance: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        """Flatten for storage. The footprint is serialised by the repository."""
        return {
            "footprint": self.footprint,
            "height_m": self.height_m,
            "source_id": self.source_id,
            "processing_job_id": self.processing_job_id,
            "method": self.method,
            "extractor": self.extractor,
            "extractor_version": self.extractor_version,
            "geometry_hash": self.geometry_hash,
            "crs": self.crs,
            "geometric_quality": self.geometric_quality,
            "extraction": {
                "method_description": METHOD_DESCRIPTION,
                "quality_metrics": self.quality_metrics,
                "provenance": self.provenance,
            },
        }


@dataclass
class ExtractionParams:
    """Full parameter set, recorded in every result's provenance."""

    ground_cell_size: float = DEFAULT_GROUND_CELL_SIZE
    ground_percentile: float = DEFAULT_GROUND_PERCENTILE
    ground_initial_threshold: float = DEFAULT_GROUND_INITIAL_THRESHOLD
    ground_min_threshold: float = DEFAULT_GROUND_MIN_THRESHOLD
    ground_max_iterations: int = DEFAULT_GROUND_MAX_ITERATIONS
    ground_neighbour_tolerance: float = DEFAULT_GROUND_NEIGHBOUR_TOLERANCE
    ground_neighbour_passes: int = DEFAULT_GROUND_NEIGHBOUR_PASSES
    min_height_above_ground: float = DEFAULT_MIN_HEIGHT_ABOVE_GROUND
    cluster_eps: float = DEFAULT_CLUSTER_EPS
    cluster_min_samples: int = DEFAULT_CLUSTER_MIN_SAMPLES
    min_cluster_points: int = DEFAULT_MIN_CLUSTER_POINTS
    min_footprint_area: float = DEFAULT_MIN_FOOTPRINT_AREA
    concave_ratio: float = DEFAULT_CONCAVE_RATIO
    simplify_tolerance: float = DEFAULT_SIMPLIFY_TOLERANCE
    height_percentile: float = DEFAULT_HEIGHT_PERCENTILE
    max_points: int = DEFAULT_MAX_POINTS

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# --------------------------------------------------------------------------
# 1. Ground classification
# --------------------------------------------------------------------------


def _cell_keys(x: np.ndarray, y: np.ndarray, cell_size: float) -> np.ndarray:
    """Integer grid cell for each point, as an ``(n, 2)`` int array."""
    return np.stack(
        [
            np.floor(np.asarray(x) / cell_size).astype(np.int64),
            np.floor(np.asarray(y) / cell_size).astype(np.int64),
        ],
        axis=1,
    )


def _grouped_percentile(
    group_index: np.ndarray, values: np.ndarray, n_groups: int, percentile: float
) -> np.ndarray:
    """Per-group percentile, fully vectorised.

    Sorts by (group, value) so that within each group the values are ascending;
    the value at rank ``r`` of group ``g`` then sits at a known offset, and two
    gathers plus linear interpolation give the percentile. No Python loop over
    cells, which matters because a city-wide scan has millions of them.
    """
    if values.size == 0:
        return np.full(n_groups, np.nan, dtype=float)

    order = np.lexsort((values, group_index))
    sorted_groups = group_index[order]
    sorted_values = values[order]

    starts = np.flatnonzero(
        np.concatenate(([True], sorted_groups[1:] != sorted_groups[:-1]))
    )
    sizes = np.diff(np.concatenate((starts, [len(sorted_groups)])))

    rank = (sizes.astype(float) - 1.0) * (percentile / 100.0)
    low_rank = np.clip(np.floor(rank).astype(int), 0, None)
    high_rank = np.clip(np.ceil(rank).astype(int), 0, None)

    low = sorted_values[starts + low_rank]
    high = sorted_values[starts + high_rank]

    result = np.empty(n_groups, dtype=float)
    result[sorted_groups[starts]] = low + (high - low) * (rank - low_rank)
    return result


def _fill_cells_without_ground(
    cell_keys: np.ndarray,
    elevations: np.ndarray,
    has_ground: np.ndarray,
    *,
    max_passes: int = 32,
) -> tuple[np.ndarray, int]:
    """Extrapolate ground into cells that contain no ground return.

    A cell lying wholly beneath a roof may contain no ground return at all, so
    its percentile lands on the *roof*; the roof is then classified as ground and
    discarded, taking the building with it. A neighbourhood-median test cannot
    fix this, because every cell under a large roof is equally wrong -- there is
    no good neighbour to compare against.

    Instead the ground surface is *dilated* outward from cells that do have a
    ground return: each empty cell adopts the mean of its filled neighbours, and
    the process repeats until no empty cells remain. Values therefore come from
    real measured ground, propagated inward along the grid -- never invented.

    Returns the filled elevations and how many cells had to be extrapolated.
    """
    n_cells = int(cell_keys.shape[0])
    if n_cells == 0:
        return elevations, 0

    filled = np.where(has_ground, elevations, np.nan)
    empty = int(np.isnan(filled).sum())
    if empty == 0 or not has_ground.any():
        # No cell anywhere has a ground return; leave the surface untouched
        # rather than fabricating one.
        return np.nan_to_num(filled, nan=float(np.nanmedian(elevations))), empty

    # Encode the (ix, iy) grid as a single sortable integer so neighbour lookup
    # is a searchsorted rather than a Python loop over cells.
    stride = int(cell_keys[:, 1].max() - cell_keys[:, 1].min()) + 1
    codes = cell_keys[:, 0] * stride + cell_keys[:, 1]
    order = np.argsort(codes)
    sorted_codes = codes[order]

    for _ in range(max_passes):
        missing = np.isnan(filled)
        if not missing.any():
            break
        sums = np.zeros(n_cells, dtype=float)
        counts = np.zeros(n_cells, dtype=float)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                target = codes + dx * stride + dy
                pos = np.clip(np.searchsorted(sorted_codes, target), 0, n_cells - 1)
                hit = (sorted_codes[pos] == target) & ~missing
                np.add.at(sums, hit, filled[pos[hit]])
                np.add.at(counts, hit, 1.0)
        # Only cells with at least one filled neighbour can be resolved.
        adopt = missing & (counts > 0)
        if not adopt.any():
            break
        filled[adopt] = sums[adopt] / counts[adopt]

    # Anything still unresolved (a fully enclosed region) takes the median of
    # what was resolved, which is at least a measured surface.
    still_missing = np.isnan(filled)
    if still_missing.any():
        known = filled[~still_missing]
        fallback = float(np.median(known)) if known.size else 0.0
        filled[still_missing] = fallback
    return filled, empty


def _reject_neighbour_outliers(
    cell_keys: np.ndarray,
    elevations: np.ndarray,
    *,
    tolerance: float = DEFAULT_GROUND_NEIGHBOUR_TOLERANCE,
    passes: int = DEFAULT_GROUND_NEIGHBOUR_PASSES,
) -> np.ndarray:
    """Smooth ground elevations that disagree with their neighbourhood.

    Complements :func:`_fill_cells_without_ground`: that handles cells with *no*
    ground return, this handles cells whose few ground points are unrepresentative
    (a stray low return under a tree, say).
    """
    if cell_keys.shape[0] == 0:
        return elevations

    n_cells = int(cell_keys.shape[0])
    stride = int(cell_keys[:, 1].max() - cell_keys[:, 1].min()) + 1
    codes = cell_keys[:, 0] * stride + cell_keys[:, 1]
    order = np.argsort(codes)
    sorted_codes = codes[order]
    sorted_values = elevations[order]

    for _ in range(max(0, passes)):
        candidates = np.full((n_cells, 8), np.nan, dtype=float)
        slot = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                target = codes + dx * stride + dy
                pos = np.clip(np.searchsorted(sorted_codes, target), 0, n_cells - 1)
                hit = sorted_codes[pos] == target
                candidates[hit, slot] = sorted_values[pos[hit]]
                slot += 1

        # A cell with no neighbours at all yields an all-NaN median; the
        # isfinite guard below discards it, so the warning is noise.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            neighbour_median = np.nanmedian(candidates, axis=1)
        outlier = np.isfinite(neighbour_median) & (
            np.abs(elevations - neighbour_median) > tolerance
        )
        if not outlier.any():
            break
        elevations = np.where(outlier, neighbour_median, elevations)

    return elevations


def classify_ground(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    *,
    cell_size: float = DEFAULT_GROUND_CELL_SIZE,
    percentile: float = DEFAULT_GROUND_PERCENTILE,
    initial_threshold: float = DEFAULT_GROUND_INITIAL_THRESHOLD,
    min_threshold: float = DEFAULT_GROUND_MIN_THRESHOLD,
    max_iterations: int = DEFAULT_GROUND_MAX_ITERATIONS,
    neighbour_tolerance: float = DEFAULT_GROUND_NEIGHBOUR_TOLERANCE,
    neighbour_passes: int = DEFAULT_GROUND_NEIGHBOUR_PASSES,
) -> tuple[np.ndarray, GroundModel]:
    """Split points into ground and non-ground with a progressive grid filter.

    The ground surface is modelled **per grid cell** from a low percentile, then
    re-estimated using only points currently classified as ground, with the
    threshold halving each pass. This converges on the true local surface and
    copes with slope far better than a single global plane would.

    Returns ``(ground_mask, model)``; the mask is ``True`` for ground points.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)
    if x.size == 0:
        empty_keys = np.zeros((0, 2), dtype=np.int64)
        model = GroundModel(
            cell_size=cell_size,
            cell_keys=empty_keys,
            cell_elevations=np.zeros(0),
            cell_index=np.zeros(0, dtype=np.int64),
        )
        return np.zeros(0, dtype=bool), model

    keys = _cell_keys(x, y, cell_size)
    unique_keys, cell_index = np.unique(keys, axis=0, return_inverse=True)
    cell_index = np.asarray(cell_index, dtype=np.int64).reshape(-1)
    n_cells = int(unique_keys.shape[0])

    # Pass 0 seeds the surface from *all* points, so roof points cannot drag it
    # upward: a low percentile is robust to them.
    cell_elevations = _grouped_percentile(cell_index, z, n_cells, percentile)

    threshold = initial_threshold
    ground_mask = np.zeros(x.size, dtype=bool)
    iterations = 0
    cells_without_ground = 0
    for iteration in range(1, max(1, max_iterations) + 1):
        above = z - cell_elevations[cell_index]
        ground_mask = above < threshold
        if not ground_mask.any():
            # Nothing qualifies as ground: keep the current surface rather than
            # collapsing it to nothing, and stop.
            break

        # Only cells that actually produced a ground point get a real estimate.
        # Every other cell is empty and will be filled from measured neighbours.
        has_ground = np.zeros(n_cells, dtype=bool)
        has_ground[cell_index[ground_mask]] = True
        refit = _grouped_percentile(
            cell_index[ground_mask], z[ground_mask], n_cells, percentile
        )
        cell_elevations, cells_without_ground = _fill_cells_without_ground(
            unique_keys, refit, has_ground
        )
        cell_elevations = _reject_neighbour_outliers(
            unique_keys, cell_elevations, tolerance=neighbour_tolerance,
            passes=neighbour_passes,
        )

        iterations = iteration
        threshold = max(min_threshold, threshold * 0.5)

    model = GroundModel(
        cell_size=cell_size,
        cell_keys=unique_keys,
        cell_elevations=cell_elevations,
        cell_index=cell_index,
        iterations=iterations,
        ground_point_count=int(ground_mask.sum()),
        final_threshold=threshold,
        cells_without_ground=cells_without_ground,
    )
    return ground_mask, model


# --------------------------------------------------------------------------
# 2. Non-ground extraction
# --------------------------------------------------------------------------


def extract_non_ground_points(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    ground_mask: np.ndarray,
    model: GroundModel,
    *,
    min_height_above_ground: float = DEFAULT_MIN_HEIGHT_ABOVE_GROUND,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(x, y, z, height_above_ground)`` for the non-ground points.

    Height is measured against the **local** ground surface rather than a global
    plane, so a sloping site still yields correct heights.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)
    height = z - model.cell_elevations[model.cell_index]
    keep = (~np.asarray(ground_mask, dtype=bool)) & (height >= min_height_above_ground)
    return x[keep], y[keep], z[keep], height[keep]


# --------------------------------------------------------------------------
# 3. Clustering (DBSCAN over XY)
# --------------------------------------------------------------------------


def cluster_building_points(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    height: np.ndarray,
    *,
    eps: float = DEFAULT_CLUSTER_EPS,
    min_samples: int = DEFAULT_CLUSTER_MIN_SAMPLES,
    min_points: int = DEFAULT_MIN_CLUSTER_POINTS,
    min_area: float = DEFAULT_MIN_FOOTPRINT_AREA,
) -> list[PointCluster]:
    """Group non-ground points into candidate structures.

    A density-based scan (DBSCAN) over XY, using a spatial hash so cost is
    near-linear rather than quadratic. Density-adaptive by design: a dense
    facade and a sparse roof edge join one cluster, while two buildings with a
    real gap between them stay separate. Border points -- reachable from a core
    point but not core themselves -- are absorbed; isolated noise is discarded.

    ``min_area`` is applied to the cluster's **bounding box** as a cheap
    pre-filter, before any hull is computed.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = int(x.size)
    if n == 0:
        return []

    eps_squared = eps * eps
    cell = max(eps, 1e-6)

    # Spatial hash. Bucket contents are stored as one array per bucket so the
    # neighbour query can gather and filter with numpy rather than a Python loop.
    bucket_keys = np.stack(
        [np.floor(x / cell).astype(np.int64), np.floor(y / cell).astype(np.int64)],
        axis=1,
    )
    order = np.lexsort((bucket_keys[:, 1], bucket_keys[:, 0]))
    sorted_keys = bucket_keys[order]
    boundaries = np.flatnonzero(
        np.any(sorted_keys[1:] != sorted_keys[:-1], axis=1)
    ) + 1
    bucket_starts = np.concatenate(([0], boundaries))
    bucket_ends = np.concatenate((boundaries, [n]))
    bucket_lookup: dict[tuple[int, int], tuple[int, int]] = {
        (int(k[0]), int(k[1])): (int(s), int(e))
        for k, s, e in zip(sorted_keys[bucket_starts], bucket_starts, bucket_ends)
    }

    def neighbour_indices(index: int) -> np.ndarray:
        cx = int(bucket_keys[index, 0])
        cy = int(bucket_keys[index, 1])
        spans = [
            bucket_lookup[(cx + dx, cy + dy)]
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if (cx + dx, cy + dy) in bucket_lookup
        ]
        if not spans:
            return np.zeros(0, dtype=np.int64)
        candidates = order[np.concatenate([np.arange(s, e) for s, e in spans])]
        dx = x[candidates] - x[index]
        dy = y[candidates] - y[index]
        within = (dx * dx + dy * dy) <= eps_squared
        return candidates[within]

    UNVISITED, NOISE = -1, -2
    labels = np.full(n, UNVISITED, dtype=np.int64)
    is_core = np.zeros(n, dtype=bool)
    cluster_id = 0

    for i in range(n):
        if labels[i] != UNVISITED:
            continue
        seeds = neighbour_indices(i)
        if seeds.size < min_samples:
            labels[i] = NOISE
            continue
        is_core[i] = True
        labels[i] = cluster_id
        queue = [int(j) for j in seeds if j != i]
        while queue:
            j = queue.pop()
            if labels[j] in (UNVISITED, NOISE):
                # Border points join the cluster but do not grow it.
                labels[j] = cluster_id
            if not is_core[j]:
                more = neighbour_indices(j)
                if more.size >= min_samples:
                    is_core[j] = True
                    for k in more:
                        if labels[k] in (UNVISITED, NOISE):
                            queue.append(int(k))
        cluster_id += 1

    clusters: list[PointCluster] = []
    for label in range(cluster_id):
        mask = labels == label
        count = int(mask.sum())
        if count < min_points:
            continue
        cx, cy = x[mask], y[mask]
        span_x = float(cx.max() - cx.min())
        span_y = float(cy.max() - cy.min())
        if span_x * span_y < min_area:
            continue
        clusters.append(
            PointCluster(
                index=len(clusters),
                x=cx,
                y=cy,
                z=np.asarray(z)[mask],
                height_above_ground=np.asarray(height)[mask],
            )
        )
    return clusters


# --------------------------------------------------------------------------
# 4-5. Footprint generation and simplification
# --------------------------------------------------------------------------


def generate_building_footprint(
    cluster: PointCluster, *, concave_ratio: float = DEFAULT_CONCAVE_RATIO
) -> "Polygon":
    """Plan footprint for one cluster, in the point cloud's own CRS.

    Prefers a concave hull so L- and U-shaped buildings are not filled in; falls
    back to the convex hull when the concave one degenerates or is smaller than
    the points' convex hull (which can happen with noisy sparse clusters).
    """
    points = geometry_service.multi_point_from_xy(cluster.x, cluster.y)
    hull: Any = None
    try:
        hull = geometry_service.concave_hull(points, ratio=concave_ratio)
    except Exception:  # noqa: BLE001 - fall back rather than fail the whole run
        hull = None
    convex = points.convex_hull

    if hull is None or hull.is_empty or hull.geom_type != "Polygon" or hull.area <= 0:
        hull = convex
    elif convex.area > 0 and hull.area > convex.area:
        # A concave hull larger than the convex hull is a numerical artefact.
        hull = convex

    if hull.geom_type != "Polygon" or hull.area <= 0:
        return _bounds_polygon(cluster)
    return hull


def generate_building_footprints(
    clusters: list[PointCluster], *, concave_ratio: float = DEFAULT_CONCAVE_RATIO
) -> list["Polygon"]:
    """Plan footprints for every cluster, in input order."""
    return [
        generate_building_footprint(cluster, concave_ratio=concave_ratio)
        for cluster in clusters
    ]


def _bounds_polygon(cluster: PointCluster, radius: float = 0.5) -> "Polygon":
    """Fallback for collinear or degenerate clusters: a small box around them."""
    min_x, min_y, max_x, max_y = cluster.bounds
    return geometry_service.create_rectangle(
        min_x - radius, min_y - radius, max_x + radius, max_y + radius
    )


def simplify_building_footprint(
    footprint: "Polygon", *, tolerance: float = DEFAULT_SIMPLIFY_TOLERANCE
) -> "Polygon":
    """Douglas-Peucker simplification, then a validity repair."""
    return geometry_service.simplify_polygon(footprint, tolerance=tolerance)


# --------------------------------------------------------------------------
# 6. Height estimation
# --------------------------------------------------------------------------


def estimate_building_height(
    cluster: PointCluster, *, percentile: float = DEFAULT_HEIGHT_PERCENTILE
) -> float:
    """Building height above local ground, in metres.

    A high percentile rather than a maximum: a maximum is set by a single noise
    spike or a tree, and would report a 4 m hut as 30 m.
    """
    if cluster.height_above_ground.size == 0:
        return 0.0
    return float(np.percentile(cluster.height_above_ground, percentile))


# --------------------------------------------------------------------------
# Quality metrics (geometric regularity, NOT accuracy)
# --------------------------------------------------------------------------


def _roof_plane_rms(cluster: PointCluster) -> float | None:
    """RMS residual of the top points from a least-squares plane.

    A flat roof gives a small residual; vegetation or a pitched roof gives a
    large one. This measures *shape*, not confidence.
    """
    heights = cluster.height_above_ground
    if heights.size < MIN_ROOF_PLANE_POINTS:
        return None

    take = max(MIN_ROOF_PLANE_POINTS, int(heights.size * ROOF_SAMPLE_FRACTION))
    top = np.argsort(heights)[-take:]
    plane_x = cluster.x[top]
    plane_y = cluster.y[top]
    plane_z = heights[top]

    if np.ptp(plane_x) < 1e-9 or np.ptp(plane_y) < 1e-9:
        # Degenerate in plan: no plane can be fitted meaningfully.
        return None

    design = np.column_stack([plane_x, plane_y, np.ones_like(plane_x)])
    try:
        solution, *_ = np.linalg.lstsq(design, plane_z, rcond=None)
    except np.linalg.LinAlgError:  # pragma: no cover - numerically pathological
        return None
    residual = plane_z - design @ solution
    return float(np.sqrt(np.mean(residual**2)))


def measure_building_quality(
    cluster: PointCluster, footprint: "Polygon", *, source_crs: str | None = None
) -> dict[str, Any]:
    """Geometric regularity of an extracted building.

    Returns the raw components plus a combined ``geometric_quality`` in [0, 1].
    **The combined score is not an accuracy and not a probability of
    correctness** -- it summarises how regular the geometry looks.
    """
    area = crs_service.calculate_metric_area(footprint, source_crs=source_crs)
    perimeter = geometry_service.polygon_perimeter(footprint, source_crs=source_crs)
    count = cluster.count

    points_per_m2 = count / area if area > 0 else 0.0
    # 1.0 for a circle, -> 0 for a sliver. A regular building is compact.
    compactness = (4.0 * math.pi * area / (perimeter**2)) if perimeter > 0 else 0.0
    compactness = min(1.0, max(0.0, compactness))

    heights = cluster.height_above_ground
    height = estimate_building_height(cluster)
    if heights.size >= 4:
        iqr = float(np.percentile(heights, 75) - np.percentile(heights, 25))
    elif heights.size:
        iqr = float(heights.max() - heights.min())
    else:  # pragma: no cover - a cluster always has points
        iqr = 0.0
    height_spread = (iqr / height) if height > 0 else 1.0

    rms = _roof_plane_rms(cluster)

    # Each component saturates at a documented point.
    planarity_score = None if rms is None else 1.0 / (1.0 + rms / 0.30)
    density_score = min(1.0, points_per_m2 / 20.0)
    stability_score = 1.0 / (1.0 + height_spread)

    parts = [
        c
        for c in (planarity_score, density_score, compactness, stability_score)
        if c is not None
    ]
    combined = float(sum(parts) / len(parts)) if parts else 0.0

    return {
        "geometric_quality": round(min(1.0, max(0.0, combined)), 4),
        "components": {
            "roof_planarity_rms_m": None if rms is None else round(rms, 4),
            "points_per_m2": round(points_per_m2, 3),
            "footprint_compactness": round(compactness, 4),
            "height_spread_ratio": round(height_spread, 4),
            "footprint_area_m2": round(area, 3),
            "footprint_perimeter_m": round(perimeter, 3),
        },
        "scores": {
            "planarity": None if planarity_score is None else round(planarity_score, 4),
            "density": round(density_score, 4),
            "compactness": round(compactness, 4),
            "height_stability": round(stability_score, 4),
        },
        "note": (
            "Geometric regularity score derived from the components above. Not an "
            "accuracy figure and not a probability of correctness."
        ),
    }


# --------------------------------------------------------------------------
# 7. Building assembly
# --------------------------------------------------------------------------


def create_building_from_point_cloud(
    cluster: PointCluster,
    *,
    source_id: str,
    processing_job_id: str,
    crs: str,
    params: ExtractionParams | None = None,
    extractor: str = "DeterministicBuildingExtractor",
    point_cloud_format: str | None = None,
    point_set_provenance: dict[str, Any] | None = None,
) -> ExtractedBuilding | None:
    """Turn one cluster into a complete extracted building, or ``None``.

    The geometry hash is computed on the footprint expressed in the engine's
    local projected plane, reached through the CRS service. Hashing the source
    coordinates instead would make the same physical building hash differently
    depending on which projected CRS the survey happened to use.

    Returns ``None`` when the cluster yields no usable building (degenerate
    footprint, or a non-positive height).
    """
    p = params or ExtractionParams()

    footprint = generate_building_footprint(cluster, concave_ratio=p.concave_ratio)
    footprint = simplify_building_footprint(footprint, tolerance=p.simplify_tolerance)
    if footprint.is_empty or footprint.area <= 0:
        return None

    height = estimate_building_height(cluster, percentile=p.height_percentile)
    if height <= 0:
        return None

    quality = measure_building_quality(cluster, footprint, source_crs=crs)
    geometry_hash = _footprint_hash(footprint, crs, height)

    return ExtractedBuilding(
        footprint=footprint,
        height_m=round(height, 3),
        source_id=source_id,
        processing_job_id=processing_job_id,
        method=EXTRACTION_METHOD,
        extractor=extractor,
        extractor_version=EXTRACTOR_VERSION,
        geometry_hash=geometry_hash,
        crs=crs,
        geometric_quality=quality["geometric_quality"],
        quality_metrics=quality,
        provenance={
            "extracted_at": now(),
            "algorithm": (
                "grid ground filter -> DBSCAN(XY) -> concave hull -> simplify"
            ),
            "method_description": METHOD_DESCRIPTION,
            "point_cloud_format": point_cloud_format,
            "source_crs": crs,
            "cluster_index": cluster.index,
            "cluster_point_count": cluster.count,
            "cluster_bounds": {
                "min_x": cluster.bounds[0],
                "min_y": cluster.bounds[1],
                "max_x": cluster.bounds[2],
                "max_y": cluster.bounds[3],
            },
            "parameters": p.to_dict(),
            "extractor": extractor,
        "extractor_version": EXTRACTOR_VERSION,
        "libraries": {"numpy": np.__version__},
            **(point_set_provenance or {}),
        },
    )


def _footprint_hash(footprint: "Polygon", crs: str, height: float) -> str:
    """Geometry hash of a footprint, CRS-independent.

    The footprint is reprojected to WGS84 and then into the engine's local
    projected plane, so a building surveyed in any projected CRS hashes the same
    as the same building surveyed in another.
    """
    local = _to_local_plane(footprint, crs)
    return geometry_service.calculate_geometry_hash(local, 0.0, height)


def _to_local_plane(footprint: "Polygon", crs: str) -> Any:
    """Footprint -> the engine's local projected plane, via the CRS service."""
    wgs84 = crs_service.transform_geometry(
        footprint, crs, geometry_service.GEOGRAPHIC_CRS
    )
    return geometry_service.wgs84_to_local(wgs84)


# --------------------------------------------------------------------------
# Processor interface
# --------------------------------------------------------------------------


class BuildingExtractor(ABC):
    """Interface for turning a point cloud into building footprints.

    Implementations must declare their ``method`` so a consumer can always tell
    an algorithmic result from a learned one.
    """

    #: e.g. ``"algorithmic_geometric"`` or ``"machine_learning"``.
    method: str = "unknown"
    name: str = "unnamed"

    @abstractmethod
    def extract(
        self,
        points: PointSet,
        *,
        source_id: str,
        processing_job_id: str,
        crs: str,
    ) -> list[ExtractedBuilding]:
        """Extract buildings from a prepared point set."""


class DeterministicBuildingExtractor(BuildingExtractor):
    """Algorithmic / geometric extractor. No model, no training, no randomness.

    Deterministic: the same points and parameters always yield the same
    footprints, which is what makes the result auditable and testable.
    """

    method = EXTRACTION_METHOD
    name = "DeterministicBuildingExtractor"

    def __init__(self, params: ExtractionParams | None = None) -> None:
        self.params = params or ExtractionParams()

    def extract(
        self,
        points: PointSet,
        *,
        source_id: str,
        processing_job_id: str,
        crs: str,
    ) -> list[ExtractedBuilding]:
        p = self.params

        ground_mask, ground_model = classify_ground(
            points.x, points.y, points.z,
            cell_size=p.ground_cell_size,
            percentile=p.ground_percentile,
            initial_threshold=p.ground_initial_threshold,
            min_threshold=p.ground_min_threshold,
            max_iterations=p.ground_max_iterations,
            neighbour_tolerance=p.ground_neighbour_tolerance,
            neighbour_passes=p.ground_neighbour_passes,
        )
        ng_x, ng_y, ng_z, ng_h = extract_non_ground_points(
            points.x, points.y, points.z, ground_mask, ground_model,
            min_height_above_ground=p.min_height_above_ground,
        )
        clusters = cluster_building_points(
            ng_x, ng_y, ng_z, ng_h,
            eps=p.cluster_eps,
            min_samples=p.cluster_min_samples,
            min_points=p.min_cluster_points,
            min_area=p.min_footprint_area,
        )

        # Provenance shared by every building from this point set: the ground
        # model only exists here, after classification.
        shared_provenance = {
            "declared_point_count": points.declared_point_count,
            "read_in_chunks": points.read_in_chunks,
            "point_set_truncated": points.truncated,
            "source_crs": points.source_crs or crs,
            "processing_crs": points.processing_crs or crs,
            "display_crs": points.display_crs,
            "ground_model": ground_model.summary(),
        }

        buildings: list[ExtractedBuilding] = []
        for cluster in clusters:
            building = create_building_from_point_cloud(
                cluster,
                source_id=source_id,
                processing_job_id=processing_job_id,
                crs=crs,
                params=p,
                extractor=self.name,
                point_cloud_format=points.format,
                point_set_provenance=shared_provenance,
            )
            if building is not None:
                buildings.append(building)
        return buildings


__all__ = [
    "DEFAULT_MAX_POINTS",
    "EXTRACTION_METHOD",
    "EXTRACTOR_VERSION",
    "METHOD_DESCRIPTION",
    "BuildingExtractor",
    "DeterministicBuildingExtractor",
    "ExtractedBuilding",
    "ExtractionParams",
    "GroundModel",
    "PointCluster",
    "PointSet",
    "classify_ground",
    "cluster_building_points",
    "create_building_from_point_cloud",
    "estimate_building_height",
    "extract_non_ground_points",
    "generate_building_footprint",
    "generate_building_footprints",
    "measure_building_quality",
    "simplify_building_footprint",
]
