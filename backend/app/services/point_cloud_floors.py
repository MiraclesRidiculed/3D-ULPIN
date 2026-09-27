"""Floor-level detection and segmentation from point-cloud elevation data.

**Algorithmic / geometric, not machine learning.** No model is trained, loaded
or applied, and nothing produced here is a learned accuracy figure. What *is*
produced is a measurement of how regular the detected storeys look, plus an
explicit list of reasons a human should check the result.

No hard-coded storey count or storey height
-------------------------------------------
The demo scene's ``8 floors at 3.2 m`` lives in :mod:`app.services.demo` and stays
there. Nothing in this module contains a floor count or a storey height. Levels
come from the points:

1. :func:`estimate_floor_height` -- floor and ceiling slabs are scanned densely
   while walls are scanned as a thin vertical spread, so a building's histogram
   of height-above-ground has a **peak at every storey**. Peak spacing is the
   storey-to-storey height, and the *median* spacing is used so one irregular
   storey cannot drag the estimate.
2. :func:`detect_floor_levels` -- levels are laid out at that height and then
   **snapped** to nearby histogram peaks, so a real building with a 3.1 m lower
   storey and 3.4 m upper ones reports the real levels rather than a uniform
   fiction. A level that cannot be snapped is kept, and marked as unsupported by
   evidence.
3. :func:`segment_points_by_floor` -- each point is assigned to the slab whose
   mid-height it falls in.
4. :func:`create_floor_volume` -- a storey becomes a footprint plus a vertical
   band, matching the app's existing volumetric record contract.
5. :func:`validate_floor_spacing` -- reports where the storeys are not uniform.

Uncertainty is surfaced, not smoothed over
------------------------------------------
:func:`segment_floor_points` returns ``requires_human_review`` with specific
reasons. The cases that genuinely cannot be resolved from geometry alone are
flagged rather than guessed:

* a top level that may be a roof slab rather than an occupiable storey;
* a storey whose height departs from the run (a double-height lobby is real, but
  a human should confirm it);
* a level with no supporting peak in the elevation histogram;
* a storey with too few points, or far more than its neighbours.

No ownership
------------
This module produces **storeys only**. It creates no property volumes, no
ownership records and no identifiers of any kind: a storey is a geometric
observation, not a right.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from app.services import crs as crs_service
from app.services import geometry as geometry_service
from app.utils import now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from shapely.geometry import Polygon

#: Recorded on every result so a consumer can never mistake this for an ML output.
SEGMENTATION_METHOD = "algorithmic_geometric"

METHOD_DESCRIPTION = (
    "Algorithmic / geometric floor segmentation. Storey levels are derived from "
    "the point elevation histogram, not from any assumed storey height or count. "
    "Not machine learning; no model was trained or applied and no accuracy figure "
    "is claimed."
)

# --------------------------------------------------------------------------
# Tunables. Distances in metres, heights above the building's base datum.
# --------------------------------------------------------------------------

#: Histogram bin size. 0.05 m is survey-grade vertical resolution, so real
#: storey structure is not smeared across adjacent bins.
DEFAULT_BIN_SIZE = 0.05
#: Smoothing window, in bins, applied before peak finding (0.25 m at 0.05 m bins).
DEFAULT_SMOOTHING_BINS = 5
#: Below this, two peaks are the same surface scanned twice, not two storeys.
#: 1.5 m is a realistic accessibility minimum for a habitable storey.
DEFAULT_MIN_STOREY_HEIGHT = 1.5
#: Above this a gap is an atrium, mezzanine or missing floor, not a storey.
DEFAULT_MAX_STOREY_HEIGHT = 8.0
#: A peak must stand this fraction of the tallest peak above the valley around
#: it to count as a storey.
DEFAULT_MIN_PEAK_PROMINENCE = 0.15
#: A ceiling may sit within this fraction of a storey height of a detected
#: surface and still count as that surface. Generous, because a level that
#: coincides with its evidence is the normal case, not an exception.
DEFAULT_SNAP_TOLERANCE_RATIO = 0.45
#: Robust top of the building. A high percentile rather than the maximum, so a
#: railing, aerial or plant room does not define the top storey's ceiling.
DEFAULT_TOP_PERCENTILE = 99.5
#: A storey height may deviate this fraction from the run before it is flagged.
DEFAULT_SPACING_TOLERANCE = 0.15
#: Storeys thinner than this fraction of the run are collapsed into a neighbour.
DEFAULT_MIN_STOREY_FRACTION = 0.40
#: A storey needs at least this fraction of the median storey point count.
DEFAULT_SPARSE_FLOOR_RATIO = 0.25
#: ...and is an outlier above this multiple of the median.
DEFAULT_DENSE_FLOOR_RATIO = 4.0
#: Minimum points for a storey to be reported at all.
DEFAULT_MIN_FLOOR_POINTS = 20
#: Below this ratio of footprint cells covered, a storey is mostly unseen.
DEFAULT_MIN_COVERAGE = 0.15
#: Points per square metre that counts as dense coverage in the quality score.
QUALITY_DENSITY_TARGET = 8.0

#: How the levels in a result were arrived at.
LEVELS_DETECTED = "detected_from_points"
LEVELS_SUPPLIED = "supplied_by_caller"
LEVELS_ASSUMED = "assumed_default"


# --------------------------------------------------------------------------
# Result carriers
# --------------------------------------------------------------------------


@dataclass
class FloorHeightEstimate:
    """Outcome of estimating storey-to-storey height from the points."""

    floor_height: float | None
    peaks: list[float] = field(default_factory=list)
    #: Per-peak support, 0-1, relative to the strongest peak.
    peak_evidence: list[float] = field(default_factory=list)
    spacings: list[float] = field(default_factory=list)
    #: Median absolute deviation of the spacings: 0 means perfectly regular.
    spacing_mad: float = 0.0
    peak_count: int = 0
    reliable: bool = False
    reason: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "floor_height_m": self.floor_height,
            "peaks_m": [round(p, 3) for p in self.peaks],
            "peak_evidence": [round(e, 4) for e in self.peak_evidence],
            "spacings_m": [round(s, 3) for s in self.spacings],
            "spacing_mad_m": round(self.spacing_mad, 4),
            "peak_count": self.peak_count,
            "reliable": self.reliable,
            "reason": self.reason,
            "parameters": self.parameters,
        }


@dataclass
class FloorLevel:
    """One detected horizontal surface: a floor slab or the building's base."""

    index: int
    #: Absolute elevation in the point cloud's own projected CRS.
    z: float
    #: Height of this level above the building's base datum.
    height_above_ground: float
    #: Where an arithmetic layout would have put it, before snapping.
    expected_z: float
    #: True when a histogram peak supported this level.
    snapped: bool
    #: Support for this level, 0-1. Zero when unsupported.
    evidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "z": round(self.z, 3),
            "height_above_ground": round(self.height_above_ground, 3),
            "expected_z": round(self.expected_z, 3),
            "snapped": self.snapped,
            "evidence": round(self.evidence, 4),
        }


@dataclass
class FloorLevels:
    """The level stack for one building, base first, top last."""

    levels: list[FloorLevel]
    base_z: float
    top_z: float
    floor_height: float
    levels_from: str = LEVELS_DETECTED
    warnings: list[str] = field(default_factory=list)

    @property
    def floor_count(self) -> int:
        """Number of storeys: one per gap between consecutive levels."""
        return max(0, len(self.levels) - 1)

    def band(self, index: int) -> tuple[float, float]:
        """Absolute ``(z_min, z_max)`` for storey ``index`` (0-based)."""
        return self.levels[index].z, self.levels[index + 1].z

    def to_dict(self) -> dict[str, Any]:
        return {
            "floor_count": self.floor_count,
            "base_z": round(self.base_z, 3),
            "top_z": round(self.top_z, 3),
            "floor_height_m": round(self.floor_height, 3),
            "levels_from": self.levels_from,
            "levels": [level.to_dict() for level in self.levels],
            "warnings": list(self.warnings),
        }


@dataclass
class SpacingFinding:
    """One departure from uniform storey height."""

    floor_number: int
    z: float
    actual_m: float
    expected_m: float
    deviation_ratio: float
    severity: str
    detail: str


@dataclass
class SpacingValidation:
    """Whether a level stack is uniformly spaced, and where it is not."""

    is_uniform: bool
    tolerance_ratio: float
    findings: list[SpacingFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_uniform": self.is_uniform,
            "tolerance_ratio": self.tolerance_ratio,
            "findings": [
                {
                    "floor_number": f.floor_number,
                    "z": round(f.z, 3),
                    "actual_m": round(f.actual_m, 3),
                    "expected_m": round(f.expected_m, 3),
                    "deviation_ratio": round(f.deviation_ratio, 4),
                    "severity": f.severity,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
        }


@dataclass
class ExtractedFloor:
    """One storey produced by segmentation."""

    floor_number: int
    z_min: float
    z_max: float
    z_min_above_ground: float
    z_max_above_ground: float
    footprint: "Polygon"
    area_m2: float
    volume_m3: float
    geometry_hash: str
    crs: str
    geometric_quality: float
    quality_metrics: dict[str, Any]
    point_count: int
    method: str
    requires_human_review: bool
    review_reasons: list[str]
    provenance: dict[str, Any]


# --------------------------------------------------------------------------
# 1. estimate_floor_height
# --------------------------------------------------------------------------


def _elevation_histogram(
    heights: np.ndarray,
    *,
    bin_size: float = DEFAULT_BIN_SIZE,
    smoothing_bins: int = DEFAULT_SMOOTHING_BINS,
) -> tuple[np.ndarray, np.ndarray]:
    """Smoothed histogram of height-above-base, and the bin centres.

    Smoothing matters: a raw histogram of a real scan is spiky enough that every
    local wobble looks like a peak.

    The range is padded with empty bins above the data. Without that, a building's
    top slab lands in the final bin, where a local-maximum test can never see it --
    and the top storey's ceiling would go undetected, quietly losing a storey.
    """
    if heights.size == 0:
        return np.zeros(0), np.zeros(0)
    top = float(heights.max())
    padding = bin_size * (smoothing_bins + 2)
    bins = max(1, int(math.ceil((top + padding) / bin_size)))
    counts, edges = np.histogram(heights, bins=bins, range=(0.0, bins * bin_size))
    smoothed = counts.astype(float)
    if smoothing_bins > 1 and smoothed.size >= smoothing_bins:
        kernel = np.ones(smoothing_bins) / smoothing_bins
        smoothed = np.convolve(smoothed, kernel, mode="same")
    centres = (edges[:-1] + edges[1:]) / 2.0
    return smoothed, centres


def _find_peaks(
    histogram: np.ndarray,
    centres: np.ndarray,
    *,
    min_prominence_ratio: float = DEFAULT_MIN_PEAK_PROMINENCE,
) -> list[tuple[float, float]]:
    """Local maxima with their prominence, as ``(height, prominence)``.

    Prominence is how far a peak stands above the higher of the two valleys on
    either side of it, which is what distinguishes a real storey slab from
    ordinary counting noise.
    """
    if histogram.size < 3:
        return []
    peaks: list[tuple[float, float]] = []
    highest = float(histogram.max())
    if highest <= 0:
        return []
    threshold = highest * min_prominence_ratio

    for i in range(1, histogram.size - 1):
        if not (histogram[i] >= histogram[i - 1] and histogram[i] > histogram[i + 1]):
            continue
        left_min = histogram[i]
        j = i - 1
        while j >= 0 and histogram[j] <= histogram[i]:
            left_min = min(left_min, histogram[j])
            j -= 1
        right_min = histogram[i]
        j = i + 1
        while j < histogram.size and histogram[j] <= histogram[i]:
            right_min = min(right_min, histogram[j])
            j += 1
        prominence = histogram[i] - max(left_min, right_min)
        if prominence > threshold:
            peaks.append((float(centres[i]), float(prominence)))
    return peaks


def _merge_close_peaks(
    peaks: list[tuple[float, float]], minimum_gap: float
) -> list[tuple[float, float]]:
    """Collapse peaks closer together than ``minimum_gap``, keeping the stronger.

    A floor slab and the ceiling above it can both produce a peak; two storeys
    closer than a habitable storey are not distinguishable.
    """
    if not peaks:
        return []
    ordered = sorted(peaks, key=lambda p: p[0])
    merged = [ordered[0]]
    for height, prominence in ordered[1:]:
        if height - merged[-1][0] < minimum_gap:
            if prominence > merged[-1][1]:
                merged[-1] = (height, prominence)
        else:
            merged.append((height, prominence))
    return merged


def estimate_floor_height(
    heights: np.ndarray,
    *,
    bin_size: float = DEFAULT_BIN_SIZE,
    smoothing_bins: int = DEFAULT_SMOOTHING_BINS,
    min_storey_height: float = DEFAULT_MIN_STOREY_HEIGHT,
    min_peak_prominence: float = DEFAULT_MIN_PEAK_PROMINENCE,
) -> FloorHeightEstimate:
    """Storey-to-storey height, estimated from the elevation histogram.

    ``heights`` are heights above the building's base. Returns an estimate whose
    ``reliable`` flag is false -- rather than a guess -- when the points do not
    contain enough structure to support one.
    """
    heights = np.asarray(heights, dtype=float)
    parameters = {
        "bin_size_m": bin_size,
        "smoothing_bins": smoothing_bins,
        "min_storey_height_m": min_storey_height,
        "min_peak_prominence_ratio": min_peak_prominence,
    }
    if heights.size == 0:
        return FloorHeightEstimate(
            None, reason="No points to estimate from", parameters=parameters
        )

    histogram, centres = _elevation_histogram(
        heights, bin_size=bin_size, smoothing_bins=smoothing_bins
    )
    raw_peaks = _find_peaks(
        histogram, centres, min_prominence_ratio=min_peak_prominence
    )
    peaks = _merge_close_peaks(raw_peaks, min_storey_height)
    # A peak below one minimum storey is the building's own base slab, not a
    # boundary between storeys. Heights here are already relative to the base, so
    # this is unambiguous. Counting it would make every building appear to have a
    # sliver storey at the bottom, and for a single-storey building it would
    # fabricate a storey height out of slab-thickness noise.
    peaks = [(h, p) for h, p in peaks if h > min_storey_height]

    if not peaks:
        return FloorHeightEstimate(
            None,
            peak_count=0,
            reason="No storey structure found in the elevation histogram",
            parameters=parameters,
        )

    highest_prominence = max(p for _, p in peaks)
    evidence = [p / highest_prominence for _, p in peaks]
    heights_found = [h for h, _ in peaks]
    spacings = [
        b - a for a, b in zip(heights_found, heights_found[1:]) if b - a > 0
    ]

    if not spacings:
        return FloorHeightEstimate(
            None,
            peaks=heights_found,
            peak_evidence=evidence,
            peak_count=len(peaks),
            reason="Only one storey structure found; a storey height needs two",
            parameters=parameters,
        )

    # Median, so one irregular storey cannot move the estimate.
    floor_height = float(np.median(spacings))
    mad = float(np.median(np.abs(np.array(spacings) - floor_height)))

    plausible = min_storey_height <= floor_height <= DEFAULT_MAX_STOREY_HEIGHT
    regular = mad <= DEFAULT_SPACING_TOLERANCE * floor_height
    reliable = bool(plausible and regular)

    reason: str | None = None
    if not plausible:
        reason = (
            f"Estimated storey height {floor_height:.2f} m is outside the plausible "
            f"range {min_storey_height}-{DEFAULT_MAX_STOREY_HEIGHT} m"
        )
    elif not regular:
        reason = (
            f"Storey spacing is irregular (median {floor_height:.2f} m, deviation "
            f"{mad:.2f} m); levels are reported but need review"
        )

    return FloorHeightEstimate(
        floor_height=floor_height,
        peaks=heights_found,
        peak_evidence=evidence,
        spacings=spacings,
        spacing_mad=mad,
        peak_count=len(peaks),
        reliable=reliable,
        reason=reason,
        parameters=parameters,
    )


# --------------------------------------------------------------------------
# 2. detect_floor_levels
# --------------------------------------------------------------------------


def detect_floor_levels(
    heights: np.ndarray,
    *,
    base_z: float = 0.0,
    floor_height: float | None = None,
    estimate: FloorHeightEstimate | None = None,
    top_percentile: float = DEFAULT_TOP_PERCENTILE,
    snap_tolerance_ratio: float = DEFAULT_SNAP_TOLERANCE_RATIO,
    min_storey_fraction: float = DEFAULT_MIN_STOREY_FRACTION,
) -> FloorLevels:
    """Lay out the level stack for a building from its points.

    **The detected surfaces are the level stack.** Where the estimate is
    trustworthy, the histogram peaks *are* the levels, so a building with a
    double-height lobby reports the real storey heights rather than a uniform
    fiction averaged over them. Only where there is no usable evidence does the
    function fall back to laying levels out arithmetically, and those levels are
    marked unsnapped so a reviewer can see they were placed rather than found.

    The top of the building is a high percentile of the observed heights, not the
    maximum: a railing or an aerial must not become a ceiling.
    """
    heights = np.asarray(heights, dtype=float)
    warnings: list[str] = []

    if estimate is None:
        estimate = estimate_floor_height(heights)

    if heights.size == 0:
        return FloorLevels(
            levels=[
                FloorLevel(0, base_z, 0.0, base_z, False, 0.0)
            ],
            base_z=base_z,
            top_z=base_z,
            floor_height=0.0,
            levels_from=LEVELS_ASSUMED,
            warnings=["No points: a single base level only"],
        )

    top_height = float(np.percentile(heights, top_percentile))
    storey = floor_height if floor_height else (estimate.floor_height or 0.0)
    levels_from = LEVELS_SUPPLIED if floor_height else LEVELS_DETECTED
    #: Below this, there is no storey worth reporting at all.
    minimum_minor = DEFAULT_MIN_STOREY_HEIGHT

    if storey <= 0:
        # No storey height could be established, but the building is clearly
        # taller than its base. Report the one storey that is unambiguously
        # present rather than claiming zero floors -- and say that the true
        # storey count could not be resolved.
        if top_height <= minimum_minor:
            return FloorLevels(
                levels=[FloorLevel(0, base_z, 0.0, base_z, False, 0.0)],
                base_z=base_z,
                top_z=base_z,
                floor_height=0.0,
                levels_from=LEVELS_ASSUMED,
                warnings=["No points above the base: no storey to report"],
            )
        return FloorLevels(
            levels=[
                FloorLevel(0, base_z, 0.0, base_z, False, 0.0),
                FloorLevel(
                    1,
                    base_z + top_height,
                    top_height,
                    base_z + top_height,
                    snapped=False,
                    evidence=0.0,
                ),
            ],
            base_z=base_z,
            top_z=base_z + top_height,
            floor_height=top_height,
            levels_from=LEVELS_ASSUMED,
            warnings=[
                "Storey height could not be estimated: only one storey structure "
                "was found, so this is reported as a single storey. A building "
                "this shape could also be several storeys with unobserved "
                "intermediate slabs and needs review."
            ],
        )

    if levels_from == LEVELS_DETECTED and not estimate.reliable and estimate.reason:
        warnings.append(estimate.reason)

    minimum_storey = max(storey * min_storey_fraction, storey * 0.25)
    # A peak too close to the base is the ground storey's own slab, not a
    # boundary between storeys: the base datum is estimated from the lowest
    # points, so it can sit a few centimetres under that slab. A peak within half
    # a minimum storey of the base is therefore noise, and keeping it would
    # invent a sliver storey at the bottom of every building.
    #
    # A peak within half a minimum storey of the top is the top surface itself;
    # it is dropped here and reintroduced as the ceiling.
    base_noise_floor = minimum_storey * 0.5
    internal: list[tuple[float, bool, float]] = []
    for peak, evidence in zip(estimate.peaks, estimate.peak_evidence):
        if base_noise_floor < peak < top_height - minimum_storey * 0.5:
            internal.append((peak, True, evidence))

    if levels_from == LEVELS_DETECTED and estimate.reliable and internal:
        entries = internal
    else:
        entries = []
        step = 1
        while True:
            expected = storey * step
            if expected >= top_height - minimum_storey:
                break
            entries.append((expected, False, 0.0))
            step += 1
            if step > 200:  # pragma: no cover - runaway guard
                break
        if not entries and estimate.peaks:
            # Unreliable estimate, but real surfaces exist: use them rather than
            # discarding the only evidence there is.
            entries = [
                (p, True, e)
                for p, e in zip(estimate.peaks, estimate.peak_evidence)
                if p < top_height - minimum_storey * 0.5
            ]
            warnings.append(
                "Storey height estimate was not reliable; levels were taken from "
                "the detected surfaces and need review"
            )

    # The building is clearly taller than the last detected surface by more than
    # a storey and a half: there is at least one storey up there with no slab
    # evidence. Place it, and say so.
    last = entries[-1][0] if entries else 0.0
    while top_height - last > storey * 1.5 and len(entries) < 200:
        last = last + storey if entries else storey
        entries.append((last, False, 0.0))
        warnings.append(
            f"Building extends {top_height - last:.2f} m above the last detected "
            f"surface at {last:.2f} m; a storey was placed arithmetically with no "
            "supporting evidence"
        )

    levels: list[FloorLevel] = [
        FloorLevel(0, base_z, 0.0, base_z, snapped=False, evidence=0.0)
    ]
    for position, (height, snapped, evidence) in enumerate(entries, start=1):
        levels.append(
            FloorLevel(
                index=position,
                z=base_z + height,
                height_above_ground=height,
                expected_z=base_z + storey * position,
                snapped=snapped,
                evidence=evidence,
            )
        )

    # The ceiling. If a detected surface sits on it, that surface is the evidence.
    top_snapped, top_evidence = _snap(
        top_height, estimate.peaks, estimate.peak_evidence, storey * snap_tolerance_ratio
    )
    levels.append(
        FloorLevel(
            index=len(levels),
            z=base_z + top_height,
            height_above_ground=top_height,
            expected_z=base_z + top_height,
            snapped=top_snapped is not None,
            evidence=top_evidence,
        )
    )

    levels = _collapse_thin_levels(levels, storey, minimum_storey, warnings)

    return FloorLevels(
        levels=levels,
        base_z=base_z,
        top_z=levels[-1].z,
        floor_height=storey,
        levels_from=levels_from,
        warnings=warnings,
    )


def _snap(
    expected: float,
    peaks: list[float],
    evidence: list[float],
    tolerance: float,
) -> tuple[float | None, float]:
    """Nearest peak to ``expected`` within ``tolerance``, and its evidence."""
    best: tuple[float, float] | None = None
    best_distance = tolerance
    for height, weight in zip(peaks, evidence):
        distance = abs(height - expected)
        if distance <= best_distance:
            best = (height, weight)
            best_distance = distance
    return best if best is not None else (None, 0.0)


def _collapse_thin_levels(
    levels: list[FloorLevel],
    storey: float,
    minimum_storey: float,
    warnings: list[str],
) -> list[FloorLevel]:
    """Remove interior levels that leave too little height for a storey."""
    kept = [levels[0]]
    for level in levels[1:]:
        gap = level.z - kept[-1].z
        if gap < minimum_storey and kept[-1].index > 0:
            warnings.append(
                f"Level at {level.height_above_ground:.2f} m was only {gap:.2f} m "
                "above the level below and was merged into it"
            )
            continue
        level.index = len(kept)
        kept.append(level)
    return kept


# --------------------------------------------------------------------------
# 3. segment_points_by_floor
# --------------------------------------------------------------------------


def segment_points_by_floor(
    heights: np.ndarray, levels: FloorLevels
) -> tuple[np.ndarray, np.ndarray]:
    """Assign each point to a storey.

    A point belongs to the slab whose **mid-height** it falls in, so a point on a
    boundary goes to the storey below it rather than being dropped or duplicated.

    Returns ``(floor_index_per_point, boundaries)`` with ``floor_index`` 0-based.
    """
    heights = np.asarray(heights, dtype=float)
    if levels.floor_count <= 0:
        return np.full(heights.size, -1, dtype=np.int64), np.zeros(0)

    boundaries = np.array(
        [
            (levels.levels[i].z + levels.levels[i + 1].z) / 2.0
            for i in range(levels.floor_count)
        ],
        dtype=float,
    )
    if boundaries.size == 0:
        return np.full(heights.size, -1, dtype=np.int64), boundaries

    # side="left" so a point exactly on a boundary joins the storey below it,
    # matching the documented rule.
    labels = np.searchsorted(boundaries, heights, side="left").astype(np.int64)
    labels = np.clip(labels, 0, levels.floor_count - 1)
    return labels, boundaries


# --------------------------------------------------------------------------
# 4. create_floor_volume
# --------------------------------------------------------------------------


def _footprint_for_floor(
    x: np.ndarray,
    y: np.ndarray,
    building_footprint: "Polygon | None",
) -> "Polygon":
    """Plan geometry for a storey.

    Prefers the building's own footprint, which is the accurate answer: a
    storey's own points may be little more than a wall ring, whose hull would be
    a thin band rather than the floor plate.

    Falls back to the **convex** hull of the storey's points. Deliberately not a
    concave hull: a storey's points include its floor slab, which densely fills
    the interior, and a concave hull over a densely filled point set is
    unstable -- measured on a 616 m2 storey it returned 265 m2. A convex hull is
    conservative but never wrong, and an L-shaped floor plate will be
    over-covered. Supplying the building footprint avoids the issue entirely.
    """
    if building_footprint is not None and not building_footprint.is_empty:
        return building_footprint
    if x.size < 3:
        return geometry_service.create_rectangle(0.0, 0.0, 0.0, 0.0)
    hull = geometry_service.multi_point_from_xy(x, y).convex_hull
    if hull.is_empty or hull.geom_type != "Polygon" or hull.area <= 0:
        return geometry_service.create_rectangle(0.0, 0.0, 0.0, 0.0)
    return hull


def _coverage_ratio(
    x: np.ndarray, y: np.ndarray, footprint: "Polygon"
) -> float:
    """Fraction of 1 m cells inside the footprint that the storey's points hit.

    A storey whose points cover only its walls is geometrically present but
    mostly unobserved, which is worth surfacing rather than hiding behind an area.
    """
    if footprint.is_empty or footprint.area <= 0 or x.size == 0:
        return 0.0
    min_x, min_y, max_x, max_y = footprint.bounds
    columns = max(1, int(max_x - min_x))
    rows = max(1, int(max_y - min_y))
    total = columns * rows
    inside = np.zeros(total, dtype=bool)
    cx = np.clip((x - min_x).astype(int), 0, columns - 1)
    cy = np.clip((y - min_y).astype(int), 0, rows - 1)
    inside[cy * columns + cx] = True
    return float(inside.sum() / total)


def create_floor_volume(
    *,
    floor_index: int,
    z_min: float,
    z_max: float,
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    building_footprint: "Polygon | None",
    crs: str,
    storey_height: float,
    level: FloorLevel,
    upper_level: FloorLevel,
    method: str = SEGMENTATION_METHOD,
) -> ExtractedFloor | None:
    """Assemble one storey from its segmented points.

    Returns ``None`` when the storey carries no usable geometry.
    """
    if z_max <= z_min:
        return None

    footprint = _footprint_for_floor(x, y, building_footprint)
    if footprint is None or footprint.is_empty or footprint.area <= 0:
        return None

    area = crs_service.calculate_metric_area(footprint, source_crs=crs)
    height = z_max - z_min
    coverage = _coverage_ratio(x, y, footprint)
    points_per_m2 = (x.size / area) if area > 0 else 0.0

    # How closely this storey's height matches the run, and how well its two
    # bounding levels are supported by the elevation histogram.
    expected = storey_height
    height_agreement = (
        1.0 / (1.0 + abs(height - expected) / max(expected, 1e-6)) if expected > 0 else 0.0
    )
    boundary_evidence = (level.evidence + upper_level.evidence) / 2.0

    components = {
        "point_count": int(x.size),
        "points_per_m2": round(points_per_m2, 3),
        "plan_coverage_ratio": round(coverage, 4),
        "height_m": round(height, 3),
        "expected_height_m": round(expected, 3),
        "height_agreement": round(height_agreement, 4),
        "lower_level_evidence": round(level.evidence, 4),
        "upper_level_evidence": round(upper_level.evidence, 4),
        "footprint_area_m2": round(area, 3),
    }
    scores = {
        "density": round(min(1.0, points_per_m2 / QUALITY_DENSITY_TARGET), 4),
        "coverage": round(min(1.0, coverage / 0.6), 4),
        "height_agreement": round(height_agreement, 4),
        "boundary_evidence": round(boundary_evidence, 4),
    }
    quality = round(sum(scores.values()) / len(scores), 4) if scores else 0.0

    local = _to_local_plane(footprint, crs)
    return ExtractedFloor(
        floor_number=floor_index + 1,
        z_min=round(z_min, 3),
        z_max=round(z_max, 3),
        z_min_above_ground=round(z_min, 3),
        z_max_above_ground=round(z_max, 3),
        footprint=footprint,
        area_m2=round(area, 2),
        volume_m3=round(area * height, 2),
        geometry_hash=geometry_service.calculate_geometry_hash(local, z_min, z_max),
        crs=crs,
        geometric_quality=quality,
        quality_metrics={
            "geometric_quality": quality,
            "components": components,
            "scores": scores,
            "note": (
                "Geometric regularity score derived from the components above. Not "
                "an accuracy figure and not a probability of correctness."
            ),
        },
        point_count=int(x.size),
        method=method,
        requires_human_review=False,
        review_reasons=[],
        provenance={},
    )


# --------------------------------------------------------------------------
# 5. validate_floor_spacing
# --------------------------------------------------------------------------


def validate_floor_spacing(
    levels: FloorLevels,
    *,
    tolerance_ratio: float = DEFAULT_SPACING_TOLERANCE,
) -> SpacingValidation:
    """Report storeys whose height departs from the run.

    A double-height lobby is a real thing, not an error, so departures are
    reported as findings for a human to confirm rather than as failures.
    """
    findings: list[SpacingFinding] = []
    storeys = [
        (levels.levels[i + 1].z - levels.levels[i].z, i)
        for i in range(levels.floor_count)
    ]
    if not storeys:
        return SpacingValidation(is_uniform=True, tolerance_ratio=tolerance_ratio)

    median_height = float(np.median([h for h, _ in storeys]))
    for actual, index in storeys:
        if median_height <= 0:
            continue
        deviation = abs(actual - median_height) / median_height
        if deviation <= tolerance_ratio:
            continue
        taller = actual > median_height
        findings.append(
            SpacingFinding(
                floor_number=index + 1,
                z=levels.levels[index + 1].z,
                actual_m=actual,
                expected_m=median_height,
                deviation_ratio=deviation,
                severity="REVIEW",
                detail=(
                    f"Storey {index + 1} is {actual:.2f} m against a run of "
                    f"{median_height:.2f} m ({deviation * 100:.0f}% "
                    f"{'taller' if taller else 'shorter'}). A double-height or "
                    "low storey is plausible; confirm it is intended."
                ),
            )
        )

    return SpacingValidation(
        is_uniform=not findings,
        tolerance_ratio=tolerance_ratio,
        findings=findings,
    )


# --------------------------------------------------------------------------
# Review flagging
# --------------------------------------------------------------------------


def _review_reasons(
    levels: FloorLevels,
    spacing: SpacingValidation,
    point_counts: list[int],
    *,
    top_level_ambiguous: bool,
    min_floor_points: int = DEFAULT_MIN_FLOOR_POINTS,
    sparse_ratio: float = DEFAULT_SPARSE_FLOOR_RATIO,
    dense_ratio: float = DEFAULT_DENSE_FLOOR_RATIO,
) -> list[str]:
    """Everything a human should check about this segmentation.

    Collected rather than collapsed into a single score, because a reviewer needs
    to know *what* to look at.
    """
    reasons: list[str] = []

    if levels.floor_count == 0:
        reasons.append("NO_FLOORS_DETECTED: no storey could be resolved")
        return reasons

    if levels.levels_from == LEVELS_SUPPLIED:
        reasons.append(
            "LEVELS_SUPPLIED: storey heights were provided by the caller, not "
            "detected from the points"
        )
    for warning in levels.warnings:
        reasons.append(f"LEVEL_WARNING: {warning}")

    unsupported = [
        level.index for level in levels.levels if not level.snapped and level.index > 0
    ]
    if unsupported:
        reasons.append(
            f"NO_LEVEL_EVIDENCE: levels {unsupported} have no supporting peak in the "
            "elevation histogram and were placed arithmetically"
        )

    if not spacing.is_uniform:
        for finding in spacing.findings:
            reasons.append(f"IRREGULAR_SPACING: {finding.detail}")

    if top_level_ambiguous:
        reasons.append(
            "TOP_LEVEL_AMBIGUOUS: the top level may be a roof slab rather than an "
            "occupiable storey. Geometry alone cannot settle this; confirm on site."
        )

    if point_counts:
        median = float(np.median(point_counts))
        if median > 0:
            sparse = [i + 1 for i, c in enumerate(point_counts) if c < median * sparse_ratio]
            dense = [i + 1 for i, c in enumerate(point_counts) if c > median * dense_ratio]
            if sparse:
                reasons.append(
                    f"SPARSE_FLOOR: storeys {sparse} have far fewer points than the "
                    "median and may be missed or wrongly split"
                )
            if dense:
                reasons.append(
                    f"DENSE_OUTLIER: storeys {dense} have far more points than the "
                    "median, which may indicate a merged double-height space"
                )

    thin = [
        i + 1 for i, c in enumerate(point_counts) if c < min_floor_points
    ]
    if thin:
        reasons.append(
            f"TOO_FEW_POINTS: storeys {thin} are below {min_floor_points} points and "
            "their extent is barely observed"
        )

    return reasons


def flag_for_review(
    levels: FloorLevels,
    spacing: SpacingValidation,
    point_counts: list[int],
    *,
    top_level_ambiguous: bool,
) -> list[str]:
    """Public entry point for review flagging, used by the service layer."""
    return _review_reasons(
        levels, spacing, point_counts, top_level_ambiguous=top_level_ambiguous
    )


# --------------------------------------------------------------------------
# Whole-building segmentation
# --------------------------------------------------------------------------


@dataclass
class FloorSegmentation:
    """A complete segmentation for one building."""

    building_index: int
    levels: FloorLevels
    estimate: FloorHeightEstimate
    spacing: SpacingValidation
    floors: list[ExtractedFloor]
    review_reasons: list[str]
    provenance: dict[str, Any]

    @property
    def requires_human_review(self) -> bool:
        return bool(self.review_reasons)

    @property
    def floor_count(self) -> int:
        return len(self.floors)


def segment_floor_points(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    *,
    base_z: float,
    building_footprint: "Polygon | None" = None,
    crs: str,
    floor_height: float | None = None,
    building_index: int = 0,
    source_id: str = "",
    processing_job_id: str = "",
    min_floor_points: int = DEFAULT_MIN_FLOOR_POINTS,
) -> FloorSegmentation:
    """Detect levels, segment points and build storeys for one building.

    ``floor_height`` may be supplied when the caller already knows it; otherwise
    it is estimated from the points and the result records which happened.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)
    started = now()

    # ``base_z`` is an absolute elevation. A caller that passes a *height* -- the
    # common slip, and one that fails silently by making every storey a whole
    # datum too tall -- is corrected here rather than producing nonsense.
    datum_shift = 0.0
    if z.size and base_z < float(z.min()):
        datum_shift = float(z.min()) - base_z
        base_z = float(z.min())

    estimate = (
        FloorHeightEstimate(
            floor_height=floor_height,
            reliable=floor_height is not None,
            reason=None if floor_height is not None else "supplied by caller",
            parameters={},
        )
        if floor_height is not None
        else estimate_floor_height(z - base_z)
    )

    heights = z - base_z
    levels = detect_floor_levels(
        heights, base_z=base_z, floor_height=floor_height, estimate=estimate
    )
    spacing = validate_floor_spacing(levels)
    labels, _ = segment_points_by_floor(z, levels)

    floors: list[ExtractedFloor] = []
    point_counts: list[int] = []
    for index in range(levels.floor_count):
        mask = labels == index
        if not mask.any():
            point_counts.append(0)
            continue
        z_min, z_max = levels.band(index)
        floor = create_floor_volume(
            floor_index=index,
            z_min=z_min,
            z_max=z_max,
            x=x[mask],
            y=y[mask],
            z=z[mask],
            building_footprint=building_footprint,
            crs=crs,
            storey_height=levels.floor_height,
            level=levels.levels[index],
            upper_level=levels.levels[index + 1],
        )
        if floor is None:
            point_counts.append(0)
            continue
        floor.provenance = {
            "segmented_at": started,
            "method_description": METHOD_DESCRIPTION,
            "source_id": source_id,
            "processing_job_id": processing_job_id,
            "building_index": building_index,
            "levels_from": levels.levels_from,
            "floor_height_m": round(levels.floor_height, 3),
            "height_estimate": estimate.to_dict(),
            "levels": levels.to_dict(),
            "spacing": spacing.to_dict(),
            "footprint_source": (
                "supplied_building_footprint"
                if building_footprint is not None
                else "hull_of_segmented_points"
            ),
            "libraries": {"numpy": np.__version__},
        }
        floors.append(floor)
        point_counts.append(floor.point_count)

    top_ambiguous = _top_level_is_ambiguous(estimate, levels, z, base_z)
    reasons = _review_reasons(
        levels,
        spacing,
        point_counts,
        top_level_ambiguous=top_ambiguous,
        min_floor_points=min_floor_points,
    )
    if datum_shift:
        reasons.append(
            f"DATUM_CORRECTED: the supplied base was {datum_shift:.2f} m below the "
            "lowest point, so it was treated as a height rather than an elevation "
            "and shifted to the lowest observed point"
        )
    per_floor_reasons = _per_floor_reasons(floors, levels, spacing, reasons)
    for floor, floor_reasons in zip(floors, per_floor_reasons):
        floor.requires_human_review = bool(floor_reasons)
        floor.review_reasons = floor_reasons

    return FloorSegmentation(
        building_index=building_index,
        levels=levels,
        estimate=estimate,
        spacing=spacing,
        floors=floors,
        review_reasons=reasons,
        provenance={
            "segmented_at": started,
            "algorithm": (
                "elevation-histogram peak spacing -> level snapping -> "
                "mid-height slab segmentation"
            ),
            "method_description": METHOD_DESCRIPTION,
            "source_id": source_id,
            "processing_job_id": processing_job_id,
            "source_crs": crs,
            "points": int(x.size),
            "base_z": round(base_z, 3),
            "base_z_corrected_by": round(datum_shift, 3),
            "top_level_ambiguous": top_ambiguous,
            "no_property_ownership": (
                "This milestone produces storeys only. No property volumes, "
                "ownership records or identifiers are created."
            ),
        },
    )


def _top_level_is_ambiguous(
    estimate: FloorHeightEstimate,
    levels: FloorLevels,
    z: np.ndarray,
    base_z: float,
) -> bool:
    """Whether the top level might be a roof rather than an occupiable storey.

    True when the top level has no supporting histogram peak, or when its
    thickness is a poor match for a storey. Both are genuinely undecidable from
    geometry, so they are flagged rather than assumed either way.
    """
    if levels.floor_count < 2:
        return True
    top = levels.levels[-1]
    if not top.snapped:
        return True
    if levels.floor_height > 0:
        thickness = top.z - levels.levels[-2].z
        if abs(thickness - levels.floor_height) / levels.floor_height > 0.25:
            return True
    return False


def _per_floor_reasons(
    floors: list[ExtractedFloor],
    levels: FloorLevels,
    spacing: SpacingValidation,
    building_reasons: list[str],
) -> list[list[str]]:
    """Attach the review reasons that bear on each individual storey."""
    per_floor: list[list[str]] = []
    spacing_by_number = {f.floor_number: f for f in spacing.findings}
    for floor in floors:
        reasons: list[str] = []
        if floor.floor_number in spacing_by_number:
            reasons.append(
                f"IRREGULAR_SPACING: {spacing_by_number[floor.floor_number].detail}"
            )
        coverage = floor.quality_metrics["components"]["plan_coverage_ratio"]
        if coverage < DEFAULT_MIN_COVERAGE:
            reasons.append(
                f"SPARSE_PLAN_COVERAGE: only {coverage * 100:.0f}% of the footprint "
                "was observed, so this storey's extent is uncertain"
            )
        if floor.point_count < DEFAULT_MIN_FLOOR_POINTS:
            reasons.append(
                f"TOO_FEW_POINTS: {floor.point_count} points is below "
                f"{DEFAULT_MIN_FLOOR_POINTS}"
            )
        lower = levels.levels[floor.floor_number - 1]
        upper = levels.levels[floor.floor_number]
        # The base is the datum, established from the ground rather than from a
        # histogram peak, so it is never "unsupported". Only interior levels are.
        unsupported = [
            name
            for name, level in (("lower", lower), ("upper", upper))
            if level.index > 0 and not level.snapped
        ]
        if unsupported:
            reasons.append(
                f"UNSUPPORTED_BOUNDARY: the {unsupported[0]} level bounding storey "
                f"{floor.floor_number} has no supporting peak in the elevation "
                "histogram"
            )
        per_floor.append(reasons)
    return per_floor


# --------------------------------------------------------------------------
# CRS helpers
# --------------------------------------------------------------------------


def _to_local_plane(footprint: "Polygon", crs: str) -> Any:
    """Footprint -> the engine's local projected plane, for the geometry hash."""
    wgs84 = crs_service.transform_geometry(
        footprint, crs, geometry_service.GEOGRAPHIC_CRS
    )
    return geometry_service.wgs84_to_local(wgs84)


__all__ = [
    "LEVELS_ASSUMED",
    "LEVELS_DETECTED",
    "LEVELS_SUPPLIED",
    "METHOD_DESCRIPTION",
    "SEGMENTATION_METHOD",
    "ExtractedFloor",
    "FloorHeightEstimate",
    "FloorLevel",
    "FloorLevels",
    "FloorSegmentation",
    "SpacingFinding",
    "SpacingValidation",
    "create_floor_volume",
    "detect_floor_levels",
    "estimate_floor_height",
    "flag_for_review",
    "segment_floor_points",
    "segment_points_by_floor",
    "validate_floor_spacing",
]
