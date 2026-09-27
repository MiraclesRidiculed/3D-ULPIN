"""Synthetic point clouds containing known buildings.

Point positions are generated from an explicit specification rather than
captured, so a test can assert the *true* footprint and height and check the
extractor recovered them. That is the only way to tell a working extractor from
one that merely returns plausible-looking polygons.

Each scene is written to a real LAS/LAZ/PLY file and read back through the normal
ingestion path, so the tests exercise file reading rather than bypassing it.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: Anchored at the demo city's projected origin, matching the point-cloud
#: fixtures, so declared CRS and coordinates genuinely agree.
#:
#: Northing matches the demo anchor (see ``app.services.geometry``). Aligning
#: matters: a building offset from the anchor hangs off the demo parcel, so the
#: end-to-end path would correctly but unhelpfully report every synthetic
#: building as overlapping a parcel boundary.
ORIGIN_E = 715_972.0
ORIGIN_N = 3_167_115.975

#: Ground plane elevation at the origin, with a gentle slope to the east.
GROUND_Z = 100.0
GROUND_SLOPE = 0.01  # metres of rise per metre east


def ground_elevation(x: np.ndarray) -> np.ndarray:
    """Ground surface: a plane tilted slightly so a global fit would be wrong."""
    return GROUND_Z + GROUND_SLOPE * (np.asarray(x) - ORIGIN_E)


@dataclass
class BuildingSpec:
    """A building to synthesise: footprint box, height, and point density."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float
    height: float
    #: Points on the roof. The rest are spread up the walls.
    roof_points: int = 400
    wall_points: int = 1600
    label: str = ""

    @property
    def area(self) -> float:
        return (self.x_max - self.x_min) * (self.y_max - self.y_min)

    def describe(self) -> str:
        return self.label or f"{self.width:.0f}x{self.depth:.0f}x{self.height:.0f}"

    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    @property
    def depth(self) -> float:
        return self.y_max - self.y_min


@dataclass
class SceneSpec:
    """A whole synthetic scan: ground coverage plus zero or more buildings."""

    buildings: list[BuildingSpec] = field(default_factory=list)
    #: Extent of the ground surface, in metres from the origin.
    x_range: tuple[float, float] = (-5.0, 95.0)
    y_range: tuple[float, float] = (-5.0, 65.0)
    ground_points: int = 4000
    ground_noise: float = 0.02
    seed: int = 7


def _generate_scene(scene: SceneSpec) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Coordinates for a whole scene: ground, walls, roofs and optional clutter."""
    rng = np.random.default_rng(scene.seed)
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    zs: list[np.ndarray] = []

    def add(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> None:
        xs.append(np.asarray(x, dtype=float))
        ys.append(np.asarray(y, dtype=float))
        zs.append(np.asarray(z, dtype=float))

    # Ground surface, offset so it never coincides with a wall/roof point.
    gx = ORIGIN_E + rng.uniform(*scene.x_range, scene.ground_points)
    gy = ORIGIN_N + rng.uniform(*scene.y_range, scene.ground_points)
    add(gx, gy, ground_elevation(gx) + rng.normal(0, scene.ground_noise, gx.size))

    for building in scene.buildings:
        # Walls: points spread uniformly from just above ground to the roof, on
        # all four faces. A real facade is scanned from its base upward, and the
        # extractor must not depend on a clean roof-only input.
        edges = [
            (np.full(building.wall_points // 4, building.x_min),
             rng.uniform(building.y_min, building.y_max, building.wall_points // 4)),
            (np.full(building.wall_points // 4, building.x_max),
             rng.uniform(building.y_min, building.y_max, building.wall_points // 4)),
            (rng.uniform(building.x_min, building.x_max, building.wall_points // 4),
             np.full(building.wall_points // 4, building.y_min)),
            (rng.uniform(building.x_min, building.x_max, building.wall_points // 4),
             np.full(building.wall_points // 4, building.y_max)),
        ]
        for ex, ey in edges:
            h = rng.uniform(0.3, building.height, ex.size)
            add(
                ORIGIN_E + ex,
                ORIGIN_N + ey,
                ground_elevation(ORIGIN_E + ex) + h,
            )
        # Roof: a flat plane at the building's height.
        rx = ORIGIN_E + rng.uniform(building.x_min, building.x_max, building.roof_points)
        ry = ORIGIN_N + rng.uniform(building.y_min, building.y_max, building.roof_points)
        add(
            rx,
            ry,
            ground_elevation(rx) + building.height + rng.normal(0, 0.01, rx.size),
        )

    return np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)


def _las_header(point_format: int = 3, crs_epsg: int | None = 32643):
    import laspy
    from pyproj import CRS as PyCRS

    header = laspy.LasHeader(point_format=point_format, version="1.4")
    header.offsets = [ORIGIN_E, ORIGIN_N, 0.0]
    header.scales = [0.01, 0.01, 0.01]
    header.generating_software = "V-CAD synthetic building fixture"
    if crs_epsg is not None:
        header.add_crs(PyCRS.from_epsg(crs_epsg))
    return header


def write_las_scene(
    path: Path, scene: SceneSpec, *, crs_epsg: int | None = 32643
) -> Path:
    """Write a scene to LAS, read back through the same path a real upload takes."""
    import laspy

    x, y, z = build_scene_arrays(scene)
    header = _las_header(crs_epsg=crs_epsg)
    las = laspy.LasData(header)
    las.x, las.y, las.z = x, y, z
    las.intensity = np.arange(x.size, dtype=np.uint16)
    las.classification = np.zeros(x.size, dtype=np.uint8)
    las.write(str(path))
    return path


def write_laz_scene(
    path: Path, scene: SceneSpec, *, crs_epsg: int | None = 32643
) -> Path:
    """Write a scene to LAZ (compressed LAS)."""
    import laspy

    x, y, z = build_scene_arrays(scene)
    las = laspy.LasData(_las_header(crs_epsg=crs_epsg))
    las.x, las.y, las.z = x, y, z
    las.intensity = np.arange(x.size, dtype=np.uint16)
    las.classification = np.zeros(x.size, dtype=np.uint8)
    las.write(str(path), do_compress=True)
    return path


def write_ply_scene(
    path: Path, scene: SceneSpec, *, crs_comment: str | None = "comment crs EPSG:32643\n"
) -> Path:
    """Write a scene to binary PLY.

    PLY declares no extent, so this exercises the path where bounds and point
    density are legitimately unavailable.
    """
    x, y, z = build_scene_arrays(scene)
    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        "comment created by V-CAD synthetic building fixture\n"
        f"{crs_comment or ''}"
        f"element vertex {x.size}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "property uchar red\n"
        "end_header\n"
    )
    body = bytearray()
    for xi, yi, zi in zip(x, y, z):
        body += struct.pack("<fff", float(xi), float(yi), float(zi))
        body += struct.pack("<B", 200)
    path.write_bytes(header.encode("ascii") + bytes(body))
    return path


# --------------------------------------------------------------------------
# Named scenes used by the tests
# --------------------------------------------------------------------------


def two_buildings_scene() -> SceneSpec:
    """Two well-separated flat-roofed buildings, 12 m and 25 m tall."""
    return SceneSpec(
        buildings=[
            BuildingSpec(5, 5, 35, 25, 12.0, label="A"),
            BuildingSpec(60, 5, 80, 25, 25.0, label="B"),
        ]
    )


def l_shape_scene() -> SceneSpec:
    """One genuinely L-shaped building, 15 m tall.

    Built as two overlapping boxes forming an L, so a convex hull would fill the
    notch and a real concave footprint will not. This is the scene that
    distinguishes a concave footprint from a lazy one.
    """
    return SceneSpec(
        buildings=[
            # Horizontal arm.
            BuildingSpec(10, 10, 50, 24, 15.0, label="L-arm"),
            # Vertical arm, offset so the union is an L with a notch at
            # (34, 24)-(50, 38).
            BuildingSpec(10, 24, 24, 46, 15.0, label="L-leg"),
        ],
        x_range=(-5.0, 60.0),
        y_range=(-5.0, 55.0),
        seed=11,
    )


def empty_scene() -> SceneSpec:
    """Ground only. The extractor must report zero buildings, not invent one."""
    return SceneSpec(buildings=[], ground_points=2500, seed=3)


def vegetation_scene() -> SceneSpec:
    """A 10 m building with a dense 18 m blob of clutter 20 m away.

    The clutter stands taller than the building. A maximum-height estimate would
    report 18 m for the building; a high percentile should not, because the two
    are separate clusters.
    """
    return add_clutter(
        SceneSpec(
            buildings=[BuildingSpec(10, 10, 34, 30, 10.0, label="house")],
            seed=5,
        ),
        x=54.0,
        y=18.0,
        radius=3.0,
        z_min=1.0,
        z_max=18.0,
        count=2500,
    )


def add_clutter(
    scene: SceneSpec,
    *,
    x: float,
    y: float,
    radius: float,
    z_min: float,
    z_max: float,
    count: int,
    seed: int = 23,
) -> SceneSpec:
    """Return a scene with an extra vertical blob of points appended.

    Used to imitate vegetation: a dense cluster that stands taller than the
    building beside it, so a naive maximum-height estimate would report the
    tree's height as the building's.
    """
    rng = np.random.default_rng(seed)
    cx = ORIGIN_E + x + rng.uniform(-radius, radius, count)
    cy = ORIGIN_N + y + rng.uniform(-radius, radius, count)
    cz = ground_elevation(cx) + rng.uniform(z_min, z_max, count)
    base_x, base_y, base_z = build_scene_arrays(scene)
    return SceneWithClutter(
        buildings=list(scene.buildings),
        x_range=scene.x_range,
        y_range=scene.y_range,
        ground_points=scene.ground_points,
        ground_noise=scene.ground_noise,
        seed=scene.seed,
        extra_x=np.concatenate([base_x, cx]),
        extra_y=np.concatenate([base_y, cy]),
        extra_z=np.concatenate([base_z, cz]),
    )


@dataclass
class SceneWithClutter(SceneSpec):
    """A scene with pre-computed coordinates, including added clutter."""

    extra_x: np.ndarray = None  # type: ignore[assignment]
    extra_y: np.ndarray = None  # type: ignore[assignment]
    extra_z: np.ndarray = None  # type: ignore[assignment]


def build_scene_arrays(scene: SceneSpec) -> tuple[np.ndarray, np.ndarray, np.ndarray]:  # noqa: F811
    """Coordinates for a scene, honouring a pre-computed clutter override."""
    override = getattr(scene, "extra_x", None)
    if override is not None:
        return override, scene.extra_y, scene.extra_z
    return _generate_scene(scene)


# --------------------------------------------------------------------------
# Multi-floor scenes
# --------------------------------------------------------------------------
# Floor segmentation is driven by the **elevation histogram**: floor and ceiling
# slabs are scanned densely, so each storey boundary produces a peak. These
# generators therefore emit a dense horizontal slab at every storey boundary plus
# a thinner vertical wall spread, which is what a real facade scan looks like.

#: Points on each horizontal slab.
SLAB_POINTS = 900
#: Points on the walls of each storey.
WALL_POINTS = 900


@dataclass
class StoreyBuilding:
    """A building defined by storey heights rather than one overall height."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float
    #: Height of each storey, base upwards. The list length is the storey count.
    storey_heights: list[float]
    label: str = ""
    #: Emit a ceiling slab at the top. A flat roof is the top storey's ceiling.
    flat_roof: bool = True

    @property
    def storey_count(self) -> int:
        return len(self.storey_heights)

    @property
    def total_height(self) -> float:
        return float(sum(self.storey_heights))

    @property
    def levels(self) -> list[float]:
        """Cumulative level heights above the base, including the top."""
        running = 0.0
        out = []
        for h in self.storey_heights:
            running += h
            out.append(running)
        return out

    @property
    def area(self) -> float:
        return (self.x_max - self.x_min) * (self.y_max - self.y_min)


def build_storey_building(
    building: StoreyBuilding, *, seed: int = 41
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Points for one multi-storey building: slabs at every level, walls between.

    Slabs are what the histogram finds, so they are dense and flat with only a
    few centimetres of noise. Walls are a thin vertical spread, which is what
    produces the low background between peaks.
    """
    rng = np.random.default_rng(seed)
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    zs: list[np.ndarray] = []

    def add(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> None:
        xs.append(np.asarray(x, dtype=float))
        ys.append(np.asarray(y, dtype=float))
        zs.append(np.asarray(z, dtype=float))

    base = 0.0
    for storey_index, storey_height in enumerate(building.storey_heights):
        top = base + storey_height
        is_top = storey_index == building.storey_count - 1

        # Slabs are **level**, referenced to the site datum, not to the local
        # ground: a real building on sloping ground is cut and filled so its
        # storeys stay level. Slabs that followed the terrain would be tilted,
        # and a test asserting storey boundaries could not tell a tilted slab
        # from a mis-detected level.
        floor_x = ORIGIN_E + rng.uniform(building.x_min, building.x_max, SLAB_POINTS)
        floor_y = ORIGIN_N + rng.uniform(building.y_min, building.y_max, SLAB_POINTS)
        add(floor_x, floor_y, GROUND_Z + base + rng.normal(0, 0.01, SLAB_POINTS))

        ceiling_z = GROUND_Z + top
        if not is_top or building.flat_roof:
            ceil_x = ORIGIN_E + rng.uniform(building.x_min, building.x_max, SLAB_POINTS)
            ceil_y = ORIGIN_N + rng.uniform(building.y_min, building.y_max, SLAB_POINTS)
            add(ceil_x, ceil_y, ceiling_z + rng.normal(0, 0.01, SLAB_POINTS))

        # Walls spanning the storey, from the local ground up to just below the
        # ceiling slab.
        per_face = WALL_POINTS // 4
        edges = [
            (np.full(per_face, building.x_min),
             rng.uniform(building.y_min, building.y_max, per_face)),
            (np.full(per_face, building.x_max),
             rng.uniform(building.y_min, building.y_max, per_face)),
            (rng.uniform(building.x_min, building.x_max, per_face),
             np.full(per_face, building.y_min)),
            (rng.uniform(building.x_min, building.x_max, per_face),
             np.full(per_face, building.y_max)),
        ]
        for ex, ey in edges:
            local_ground = ground_elevation(ORIGIN_E + ex)
            # Interpolate so the wall meets the level slab exactly, and starts at
            # the terrain rather than at the site datum.
            u = rng.uniform(0.03, 0.97, ex.size)
            add(ORIGIN_E + ex, ORIGIN_N + ey, local_ground + u * (ceiling_z - local_ground))

        base = top

    return np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)


def storey_scene(
    building: StoreyBuilding,
    *,
    ground_points: int = 4000,
    seed: int = 41,
) -> SceneSpec:
    """A full scan: site ground plus one multi-storey building."""
    rng = np.random.default_rng(seed + 1)
    gx = ORIGIN_E + rng.uniform(
        building.x_min - 8, building.x_max + 8, ground_points
    )
    gy = ORIGIN_N + rng.uniform(
        building.y_min - 8, building.y_max + 8, ground_points
    )
    gz = ground_elevation(gx) + rng.normal(0, 0.02, gx.size)

    bx, by, bz = build_storey_building(building, seed=seed)
    return SceneWithClutter(
        buildings=[],
        x_range=(-5.0, 95.0),
        y_range=(-5.0, 65.0),
        ground_points=ground_points,
        ground_noise=0.02,
        seed=seed,
        extra_x=np.concatenate([gx, bx]),
        extra_y=np.concatenate([gy, by]),
        extra_z=np.concatenate([gz, bz]),
    )


def three_storey_building() -> StoreyBuilding:
    """Three storeys, 3.5 m each, flat roof. Total 10.5 m."""
    return StoreyBuilding(
        x_min=10.0, y_min=10.0, x_max=40.0, y_max=34.0,
        storey_heights=[3.5, 3.5, 3.5], label="three-storey",
    )


def five_storey_building() -> StoreyBuilding:
    """Five storeys, 3.2 m each. Total 16.0 m."""
    return StoreyBuilding(
        x_min=8.0, y_min=8.0, x_max=36.0, y_max=30.0,
        storey_heights=[3.2, 3.2, 3.2, 3.2, 3.2], label="five-storey",
    )


def single_storey_building() -> StoreyBuilding:
    """One storey, 4.2 m, flat roof. Total 4.2 m."""
    return StoreyBuilding(
        x_min=12.0, y_min=12.0, x_max=34.0, y_max=30.0,
        storey_heights=[4.2], label="single-storey",
    )


def double_height_building() -> StoreyBuilding:
    """Four storeys where the second is double height: 3.4, 6.9, 3.4, 3.4.

    Levels land at 3.4, 10.3, 13.7 and 17.1 m. A uniform-grid approach would
    average the 6.9 m storey away; this scene is what proves the levels come from
    the data.
    """
    return StoreyBuilding(
        x_min=10.0, y_min=10.0, x_max=38.0, y_max=32.0,
        storey_heights=[3.4, 6.9, 3.4, 3.4], label="double-height",
    )


def pitched_roof_building() -> StoreyBuilding:
    """Three storeys under a pitched roof, so the roof is not a flat slab.

    The apex is a gable ridge above the top storey's ceiling, so a naive
    "top of the building is a ceiling" rule would invent a fourth storey.
    """
    return StoreyBuilding(
        x_min=10.0, y_min=10.0, x_max=36.0, y_max=30.0,
        storey_heights=[3.3, 3.3, 3.3], label="pitched-roof",
        flat_roof=False,
    )


def write_las_storey_scene(
    path: Path, building: StoreyBuilding, **kwargs
) -> Path:
    """Write a multi-storey scene to LAS, for the full upload-then-segment path."""
    return write_las_scene(path, storey_scene(building, **kwargs))


__all__ = [
    "GROUND_SLOPE",
    "GROUND_Z",
    "ORIGIN_E",
    "ORIGIN_N",
    "SLAB_POINTS",
    "WALL_POINTS",
    "build_las_storey_scene",
    "write_las_storey_scene",
    "BuildingSpec",
    "SceneSpec",
    "SceneWithClutter",
    "StoreyBuilding",
    "add_clutter",
    "build_scene_arrays",
    "build_storey_building",
    "double_height_building",
    "empty_scene",
    "five_storey_building",
    "ground_elevation",
    "pitched_roof_building",
    "single_storey_building",
    "storey_scene",
    "three_storey_building",
    "empty_scene",
    "ground_elevation",
    "l_shape_scene",
    "two_buildings_scene",
    "vegetation_scene",
    "write_las_scene",
    "write_laz_scene",
    "write_ply_scene",
]
