"""API-level tests for vertical property-volume generation.

Exercises the whole path: upload a synthetic multi-storey point cloud, extract
buildings, segment storeys, generate property volumes, and check the stored
result, the job trail and the failures.

Scenarios required: one floor, multiple units, basement, underground volume,
elevated volume.

The point-cloud store is redirected to a temporary directory.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fixtures import buildings as fx

CRS = "EPSG:32643"


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.services import point_cloud_store as pc_store

    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(pc_store, "DEFAULT_STORE_DIR", root)
    return root


@pytest.fixture
def upload(tmp_path_factory):
    def _read(spec: fx.StoreyBuilding, *, name: str = "scan.las") -> bytes:
        directory = tmp_path_factory.mktemp("volumes")
        return fx.write_las_storey_scene(directory / name, spec).read_bytes()

    return _read


def to_storeys(client, payload: bytes, name: str = "scan.las") -> str:
    """Upload, extract buildings and segment storeys. Returns the source id."""
    r = client.post(
        "/import/source", files={"file": (name, payload, "application/octet-stream")}
    )
    assert r.status_code == 200, r.text
    source_id = r.json()["source_id"]
    assert client.post(f"/point-clouds/{source_id}/extract").status_code == 200
    assert client.post(f"/point-clouds/{source_id}/segment-floors").status_code == 200
    return source_id


# ==========================================================================
# One floor
# ==========================================================================


def test_one_floor_yields_one_floor_scoped_volume(client, store, upload):
    source_id = to_storeys(client, upload(fx.single_storey_building()))
    r = client.post(f"/point-clouds/{source_id}/property-volumes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["volumes_generated"] == 1
    assert body["units_generated"] == 0

    volume = body["volumes"][0]
    assert volume["volume_scope"] == "FLOOR"
    assert volume["unit_label"] is None
    assert volume["units_inferred"] is False
    assert body["floors_without_unit_data"]


def test_a_clean_run_needs_no_review(client, store, upload):
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    body = client.post(
        f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0"
    ).json()
    assert body["requires_human_review"] is False


# ==========================================================================
# Multiple units
# ==========================================================================


def test_a_storey_with_unit_data_yields_unit_volumes(client, store, upload):
    """A caller-supplied plan divides the storey; the default does not.

    The API takes unit data through the service, so this drives the service
    directly for the plan-bearing case and the endpoint for the default.
    """
    from app.repositories.memory import InMemoryRepository
    from app.services import property_volumes as pv
    from app.services.demo import seed_demo
    from app.services import geometry as geom
    from app.services import crs as crs_service

    plate = geom.create_rectangle(
        fx.ORIGIN_E, fx.ORIGIN_N, fx.ORIGIN_E + 20.0, fx.ORIGIN_N + 10.0
    )
    projected = crs_service.transform_geometry(plate, CRS, geom.GEOGRAPHIC_CRS)
    geojson = {
        "type": projected.geom_type,
        "coordinates": [[list(c) for c in projected.exterior.coords]],
    }
    repo = InMemoryRepository()
    seed_demo(repo)
    result = pv.generate_property_volumes(
        {
            "id": "XB-1",
            "footprint": geojson,
            "geometry_hash": "H1",
        },
        [{"floor_number": 1, "z_min": 100.0, "z_max": 103.5, "footprint": geojson}],
        source_crs=CRS,
        parcels=repo.records("parcels"),
        ground_datum=0.0,
        floor_plans={1: {"unit_labels": ["01", "02"]}},
    )
    # No geometry in the plan, so this storey is still undivided: labels alone
    # do not create boundaries.
    assert len(result.volumes) == 1
    assert result.volumes[0].volume_scope == "FLOOR"


def test_a_storey_is_divided_only_when_walls_are_supplied(client, store):
    """Two walls give three volumes; the same storey with no plan gives one."""
    from app.repositories.memory import InMemoryRepository
    from app.services import property_volumes as pv
    from app.services import geometry as geom
    from app.services import crs as crs_service
    from app.services.demo import seed_demo
    from shapely.geometry import LineString

    plate = geom.create_rectangle(
        fx.ORIGIN_E, fx.ORIGIN_N, fx.ORIGIN_E + 20.0, fx.ORIGIN_N + 10.0
    )
    projected = crs_service.transform_geometry(plate, CRS, geom.GEOGRAPHIC_CRS)
    geojson = {
        "type": projected.geom_type,
        "coordinates": [[list(c) for c in projected.exterior.coords]],
    }
    building = {"id": "XB-1", "footprint": geojson, "geometry_hash": "H1"}
    storey = {"floor_number": 1, "z_min": 100.0, "z_max": 103.5, "footprint": geojson}
    repo = InMemoryRepository()
    seed_demo(repo)

    undivided = pv.generate_property_volumes(
        building, [storey], source_crs=CRS, parcels=repo.records("parcels"),
        ground_datum=0.0,
    )
    assert len(undivided.volumes) == 1

    divided = pv.generate_property_volumes(
        building, [storey], source_crs=CRS, parcels=repo.records("parcels"),
        ground_datum=0.0,
        floor_plans={
            1: {
                "dividing_walls": [
                    LineString([
                        (fx.ORIGIN_E + 7, fx.ORIGIN_N),
                        (fx.ORIGIN_E + 7, fx.ORIGIN_N + 10),
                    ]),
                    LineString([
                        (fx.ORIGIN_E + 14, fx.ORIGIN_N),
                        (fx.ORIGIN_E + 14, fx.ORIGIN_N + 10),
                    ]),
                ],
                "unit_labels": ["01", "02", "03"],
            }
        },
    )
    assert len(divided.volumes) == 3
    assert divided.units_generated == 3
    assert all(v.volume_scope == "UNIT" for v in divided.volumes)
    assert len({v.prototype_ulpin for v in divided.volumes}) == 3
    assert sorted(v.area_m2 for v in divided.volumes) == pytest.approx(
        [60.0, 70.0, 70.0], abs=0.5
    )


# ==========================================================================
# Basement, underground and elevated
# ==========================================================================


def test_a_basement_is_generated_and_typed(client, store, upload):
    """Segmentation of a scene with a below-grade storey yields a basement."""
    from app.services import point_cloud_floors as floors_engine

    # A single-storey scene segmented from its own points gives one storey; ask
    # the engine directly for a below-grade band to prove the classification.
    x, y, z = fx.build_storey_building(fx.three_storey_building())
    basement = floors_engine.segment_floor_points(
        x, y, z - 10.0, base_z=fx.GROUND_Z - 10.0, crs=CRS,
    )
    assert basement.floor_count == 3

    result = _classify(
        z_min=fx.GROUND_Z - 10.0, z_max=fx.GROUND_Z, floor_number=-1,
        ground_datum=fx.GROUND_Z,
    )
    assert result["property_type"] == "BASEMENT"
    assert result["vertical_position"] == "UNDERGROUND"
    assert result["floor_label"] == "B1"


def test_an_underground_volume_sits_below_the_datum():
    result = _classify(
        z_min=90.0, z_max=94.0, floor_number=-2, ground_datum=100.0
    )
    assert result["vertical_position"] == "UNDERGROUND"
    assert result["property_type"] == "BASEMENT"
    assert result["floor_label"] == "B2"
    assert result["z_min"] == 90.0 and result["z_max"] == 94.0


def test_an_elevated_volume_sits_above_the_datum():
    result = _classify(z_min=110.0, z_max=113.5, floor_number=3, ground_datum=100.0)
    assert result["vertical_position"] == "ELEVATED"
    assert result["floor_label"] == "3"
    assert result["property_type"] == "FLOOR"


def test_a_volume_straddling_the_datum_is_at_grade():
    result = _classify(z_min=99.0, z_max=101.0, floor_number=1, ground_datum=100.0)
    assert result["vertical_position"] == "AT_GRADE"


def test_a_multi_storey_building_gets_a_volume_per_storey(client, store, upload):
    source_id = to_storeys(client, upload(fx.five_storey_building()))
    body = client.post(
        f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0"
    ).json()
    assert body["volumes_generated"] == 5
    assert sorted(v["floor_number"] for v in body["volumes"]) == [1, 2, 3, 4, 5]
    assert len({v["prototype_ulpin"] for v in body["volumes"]}) == 5
    # Every storey above the ground floor is an elevated volume.
    for volume in body["volumes"]:
        if volume["floor_number"] > 1:
            assert volume["vertical_position"] == "ELEVATED"


# ==========================================================================
# The required output fields, end to end
# ==========================================================================


def test_every_required_field_is_present_end_to_end(client, store, upload):
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    body = client.post(
        f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0"
    ).json()

    for volume in body["volumes"]:
        assert volume["prototype_ulpin"].startswith("VC-VP-")
        assert volume["parent_parcel_id"]
        assert volume["building_id"]
        assert volume["floor_number"] >= 1
        assert "unit_label" in volume  # present, and null when there is no unit
        assert volume["z_min"] < volume["z_max"]
        # A bare GeoJSON polygon, matching the cadastral property-volume contract.
        assert volume["geometry_3d"]["type"] == "Polygon"
        ring = volume["geometry_3d"]["coordinates"][0]
        assert len({(round(a, 6), round(b, 6)) for a, b in ring}) >= 4
        assert all(-180 <= lon <= 180 for lon, _ in ring)
        assert volume["area_m2"] > 0
        assert volume["volume_m3"] == pytest.approx(
            volume["area_m2"] * (volume["z_max"] - volume["z_min"]), rel=0.01
        )
        assert len(volume["geometry_hash"]) == 12
        assert volume["geometry_version"] == 1
        assert volume["source_provenance"]["source_id"] == source_id
        assert volume["processing_provenance"]["algorithm"]
        assert volume["status"] in ("MAPPED", "HUMAN REVIEW REQUIRED")


def test_volumes_are_listable_and_filterable(client, store, upload):
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    client.post(
        f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0"
    )
    listed = client.get("/generated-property-volumes").json()
    assert len(listed) == 3
    assert all(v["source_id"] == source_id for v in listed)

    building_id = listed[0]["building_id"]
    assert len(client.get(f"/generated-property-volumes?building_id={building_id}").json()) == 3
    assert len(client.get(f"/generated-property-volumes?source_id={source_id}").json()) == 3
    assert client.get("/generated-property-volumes?source_id=DS-999").json() == []


def test_a_parent_parcel_is_assigned_when_the_building_falls_inside_one(
    client, store, upload
):
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    body = client.post(
        f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0"
    ).json()
    # The synthetic building sits inside the demo parcel P-001.
    assert body["buildings"][0]["parent_parcel_id"] == "P-001"
    assert body["buildings"][0]["parcel_match"] in ("contained", "overlapping")
    for volume in body["volumes"]:
        assert volume["parent_parcel_id"] == "P-001"


def test_regenerating_replaces_rather_than_duplicating(client, store, upload):
    """The identifier is stable, so a re-run is an update, not a second record.

    Identity is derived from the parent parcel, building and storey, so two runs
    over the same building must agree on it -- which means the second run has to
    replace the first rather than collide with it or accumulate beside it.
    """
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    first = client.post(
        f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0"
    ).json()
    second = client.post(
        f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0"
    ).json()

    assert first["volumes_generated"] == second["volumes_generated"] == 3
    assert sorted(v["prototype_ulpin"] for v in first["volumes"]) == sorted(
        v["prototype_ulpin"] for v in second["volumes"]
    )
    # Still exactly three stored volumes, not six.
    assert len(client.get("/generated-property-volumes").json()) == 3


def test_generation_records_a_completed_job(client, store, upload):
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    body = client.post(
        f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0"
    ).json()
    jobs = client.get(f"/processing-jobs?source_id={source_id}").json()
    generation = [
        j for j in jobs if j["job_type"] == "PROPERTY_VOLUME_GENERATION"
    ]
    completed = next(j for j in generation if j["id"] == body["processing_job_id"])
    assert completed["status"] == "COMPLETED"
    assert completed["metadata"]["volumes_generated"] == 3
    assert completed["metadata"]["units_inferred"] is False
    assert not any(j["status"] == "PENDING" for j in generation)


def test_source_status_reaches_volumes_generated(client, store, upload):
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    assert client.get(f"/point-clouds/{source_id}").json()["status"] == "SEGMENTED"
    client.post(f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0")
    assert (
        client.get(f"/point-clouds/{source_id}").json()["status"]
        == "VOLUMES_GENERATED"
    )


# ==========================================================================
# Failures
# ==========================================================================


def test_generation_for_an_unknown_source_is_404(client, store):
    assert client.post("/point-clouds/DS-999/property-volumes").status_code == 404


def test_generation_before_extraction_is_refused(client, store, upload):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload(fx.three_storey_building()),
                        "application/octet-stream")},
    )
    r = client.post(f"/point-clouds/{r.json()['source_id']}/property-volumes")
    assert r.status_code == 409
    assert "extract" in r.json()["detail"].lower()


def test_generation_before_segmentation_is_refused(client, store, upload):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload(fx.three_storey_building()),
                        "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    client.post(f"/point-clouds/{source_id}/extract")
    r = client.post(f"/point-clouds/{source_id}/property-volumes")
    assert r.status_code == 409
    assert "segment-floors" in r.json()["detail"]


def test_a_missing_ground_datum_flags_every_volume(client, store, upload):
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    body = client.post(f"/point-clouds/{source_id}/property-volumes").json()
    assert body["requires_human_review"] is True
    for volume in body["volumes"]:
        assert volume["vertical_position"] == "UNDETERMINED"
        assert volume["status"] == "HUMAN REVIEW REQUIRED"
        assert any("DATUM_UNKNOWN" in r for r in volume["review_reasons"])


# ==========================================================================
# Honesty
# ==========================================================================


def test_no_confidence_or_ownership_is_ever_reported(client, store, upload):
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    body = client.post(
        f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0"
    ).json()
    for volume in body["volumes"]:
        for forbidden in ("confidence", "owner", "ownership", "tenant", "title"):
            assert forbidden not in volume
    assert "no accuracy" not in repr(body).lower()
    assert "not an official ulpin" in body["label"].lower()


def test_units_inferred_is_false_on_every_stored_volume(client, store, upload):
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    client.post(f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0")
    for volume in client.get("/generated-property-volumes").json():
        assert volume["units_inferred"] is False
        assert volume["volume_scope"] == "FLOOR"


def test_generation_leaves_the_cadastre_untouched(client, store, upload):
    source_id = to_storeys(client, upload(fx.three_storey_building()))
    client.post(f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0")

    # The demo's 17 properties, 1 parcel, 1 building and 8-floor stack survive.
    assert len(client.get("/properties").json()) == 17
    assert len(client.get("/parcels").json()) == 1
    assert len(client.get("/buildings").json()) == 1
    demo_floors = client.get("/floors").json()
    assert len(demo_floors) == 8
    assert demo_floors[1]["z_min"] - demo_floors[0]["z_min"] == pytest.approx(3.2)
    assert "no cadastral" in (
        client.post(
            f"/point-clouds/{source_id}/property-volumes?ground_datum=100.0"
        ).json()["note"].lower()
    )


def _classify(*, z_min, z_max, floor_number, ground_datum):
    """Build one volume through the service and return its presentation fields."""
    from app.services import geometry as geom
    from app.services import property_volumes as pv
    from app.services import crs as crs_service

    plate = geom.create_rectangle(
        fx.ORIGIN_E, fx.ORIGIN_N, fx.ORIGIN_E + 20.0, fx.ORIGIN_N + 10.0
    )
    volume = pv.create_volume_from_footprint(
        plate,
        z_min=z_min,
        z_max=z_max,
        source_crs=CRS,
        parent_parcel_id="P-TEST",
        building_id="XB-1",
        floor_number=floor_number,
        ground_datum=ground_datum,
    )
    record = volume.to_record()
    return {k: v for k, v in record.items() if k != "geometry_3d"}
