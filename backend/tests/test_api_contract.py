"""API contract regression tests.

These lock the externally observable behaviour of the API, including the
deliberate validation findings in the demo scene. The demo depends on those
three conflicts existing, so a change that removes them is a regression, not a
cleanup.

Response shapes here were captured from the original monolithic
``app/main.py`` and must not drift without an intentional API version bump.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.test_validation_engine import ALL_RULE_IDS

#: Stable prototype ULPIN shape: VC-LP-{parcel} or VC-VP-{parent}-{unit}.
#: Deliberately carries no geometry-derived component.
ULPIN_PATTERN = re.compile(r"^VC-(?:LP|VP)-[A-Z0-9]+(?:-[A-Z0-9]+)?$")

#: The three intentional demo findings, in the order the UI displays them.
#: Overlap values are the *true* geometric answers, checked with a tolerance
#: rather than pinned: measurements run after a 7-dp WGS84 round-trip, so exact
#: equality would be brittle and would hide a genuine regression behind a
#: coordinate change.
EXPECTED_ISSUES = [
    # id, severity, issue type, overlap_volume, relative tolerance
    ("VAL-OUT-PV-502", "WARNING", "OUTSIDE_PARENT_PARCEL", 0.0, 0.0),
    # 180 m^2 of plan overlap x 3.2 m of shared height
    ("VAL-OVR-PV-201-PV-202", "CRITICAL", "VERTICAL_VOLUME_OVERLAP", 576.0, 5e-3),
    # 176 m^2 x 2.2 m
    ("VAL-INF-PV-B001", "CRITICAL", "UNDERGROUND_INFRASTRUCTURE_COLLISION", 387.2, 5e-3),
]


def test_health(client):
    """Health must describe the active backend without breaking either way."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # `storage` names whichever backend is active; both are valid answers.
    assert body["storage"] in ("local deterministic demo store", "postgresql+postgis")
    assert "database" in body
    # When PostGIS is configured it must actually report on it.
    if body["storage"] == "postgresql+postgis":
        assert body["database"]["postgis"] is True
        assert body["database"]["error"] is None


def test_demo_load_seeds_scene(client):
    r = client.get("/demo/load")
    assert r.status_code == 200
    assert r.json() == {
        "message": "Demo City loaded",
        "properties": 17,
        "issues": 3,
    }


def test_parcels(client):
    body = client.get("/parcels").json()
    assert len(body) == 1
    parcel = body[0]
    assert parcel["parcel_id"] == "P-001"
    assert parcel["land_use"] == "Mixed residential"
    assert parcel["area"] == 4160.0
    # geometry_hash was written internally and stripped by response_model=Parcel,
    # while geometry_version was published. That is half a version: a revision
    # number that cannot be matched to the geometry it describes says nothing
    # about whether a shape moved. The audit made it public for that reason.
    assert parcel["geometry_hash"], "a parcel must publish the hash its version counts"
    assert len(parcel["geometry_hash"]) == 12
    assert parcel["geometry_version"] >= 1
    # Identity is independent of both, and unchanged by either.
    assert parcel["prototype_ulpin"] == "VC-LP-P001"


def test_buildings(client):
    body = client.get("/buildings").json()
    assert len(body) == 1
    b = body[0]
    assert b["building_id"] == "B-001"
    assert b["parcel_id"] == "P-001"
    assert b["floor_count"] == 8
    assert b["height"] == 25.6
    # No fabricated accuracy. The seed used to write confidence=0.974 here and
    # the API served it; asserting the key is absent means a future edit that
    # puts a literal back fails rather than passing unnoticed.
    assert "confidence" not in b


def test_properties_shape(client):
    body = client.get("/properties").json()
    # 16 apartments + 1 basement
    assert len(body) == 17
    apartments = [p for p in body if p["property_type"] == "APARTMENT"]
    basement = [p for p in body if p["property_type"] == "BASEMENT_PARKING"]
    assert len(apartments) == 16
    assert len(basement) == 1
    assert basement[0]["z_min"] == -3.2 and basement[0]["z_max"] == 0
    for p in body:
        assert ULPIN_PATTERN.match(p["prototype_ulpin"]), p["prototype_ulpin"]
        assert len(p["geometry_hash"]) == 12
        assert p["geometry_hash"].isupper()


def test_preserved_deliberate_demo_errors(client):
    """The demo's three showcase conflicts must survive."""
    body = client.get("/validation/issues").json()
    assert len(body) == 3, [i["id"] for i in body]
    for got, (ident, severity, issue_type, overlap, tol) in zip(body, EXPECTED_ISSUES):
        assert got["id"] == ident
        assert got["severity"] == severity
        assert got["issue_type"] == issue_type
        assert got["overlap_volume"] == pytest.approx(overlap, rel=tol, abs=0.5)

    by_id = {i["id"]: i for i in body}
    # Floor 5 apartment extends 6 m past the parcel edge at x=80, over 36 m.
    encroach = by_id["VAL-OUT-PV-502"]
    assert encroach["geometry"] is not None
    assert "outside parent parcel P-001" in encroach["description"]
    # Floor 2 apartments overlap each other.
    overlap_issue = by_id["VAL-OVR-PV-201-PV-202"]
    assert overlap_issue["object_a"] == "PV-201"
    assert overlap_issue["object_b"] == "PV-202"
    # Utility corridor intersects the basement.
    infra_issue = by_id["VAL-INF-PV-B001"]
    assert infra_issue["object_b"] == "INF-U-001"

    # Floor 5 carries the human-review status.
    floor5 = [p for p in client.get("/properties").json() if p["id"] == "PV-502"][0]
    assert floor5["status"] == "HUMAN REVIEW REQUIRED"
    assert floor5["floor_number"] == 5


def test_validation_run_is_idempotent(client):
    first = client.post("/validation/run").json()
    second = client.post("/validation/run").json()
    assert first == second == {"issues": 3, "critical": 2, "warning": 1}


def test_issues_expose_rule_attribution_and_evidence(client):
    """The rule engine's output reaches the API, additively."""
    for issue in client.get("/validation/issues").json():
        assert issue["rule_id"] in ALL_RULE_IDS
        assert issue["category"] in {
            "GEOMETRY",
            "PARCEL",
            "VERTICAL",
            "CADASTRAL",
            "INFRASTRUCTURE",
            "CHANGE",
        }
        assert issue["evidence"]
    encroach = next(
        i for i in client.get("/validation/issues").json() if i["id"] == "VAL-OUT-PV-502"
    )
    assert encroach["rule_id"] == "PARCEL-PROPERTY-OUTSIDE"
    assert encroach["evidence"]["outside_area_m2"] > 0.01


def test_validation_rules_endpoint_lists_every_rule(client):
    body = client.get("/validation/rules").json()
    assert [r["rule_id"] for r in body] == ALL_RULE_IDS
    assert set(body[0]) == {
        "rule_id",
        "category",
        "issue_type",
        "severity",
        "description",
        "collections",
    }


def test_validation_rules_endpoint_needs_no_seeded_scene(client):
    """It reads no store, so it is safe before anything exists."""
    body = client.get("/validation/rules").json()
    assert len(body) == 30


def test_single_rule_run_does_not_touch_the_stored_findings(client):
    before = client.get("/validation/issues").json()
    body = client.post("/validation/rules/VERT-PROPERTY-OVERLAP/run").json()
    assert body["rule_id"] == "VERT-PROPERTY-OVERLAP"
    assert body["count"] == 1
    assert body["findings"][0]["object_a"] == "PV-201"
    assert client.get("/validation/issues").json() == before


def test_single_rule_run_rejects_an_unknown_rule(client):
    assert client.post("/validation/rules/NOT-A-RULE/run").status_code == 404


def test_validation_summary_counts_the_stored_findings(client):
    body = client.get("/validation/summary").json()
    assert body["total"] == 3
    assert body["source"] == "stored"
    assert body["rules_evaluated"] == 30
    assert sum(body["by_severity"].values()) == 3
    assert body["by_rule"] == {
        "PARCEL-PROPERTY-OUTSIDE": 1,
        "VERT-PROPERTY-OVERLAP": 1,
        "INFRA-UTILITY-COLLISION": 1,
    }


def test_validation_summary_is_safe_on_an_empty_store(client):
    """It reads stored findings, so it must not run the seeded-scene rules."""
    from app.repositories import get_repository

    repository = get_repository()
    repository.clear("issues")
    body = client.get("/validation/summary").json()
    assert body["total"] == 0
    assert body["by_severity"] == {}


def test_analytics_summary(client):
    body = client.get("/analytics/summary").json()
    assert body["parcels"] == 1
    assert body["buildings"] == 1
    assert body["property_volumes"] == 17
    assert body["apartments"] == 16
    assert body["underground_assets"] == 1
    assert body["validation_issues"] == 3
    assert body["critical_conflicts"] == 2
    # average_confidence was the mean of a field every record carried as one
    # of two literals, so it was a constant dressed as a statistic. It is gone,
    # and nothing here replaces it: no accuracy is measured anywhere.
    assert "average_confidence" not in body
    assert not [k for k in body if "confidence" in k or "accuracy" in k]
    assert body["distribution"][0] == {"level": "Utility", "count": 1}
    assert len(body["distribution"]) == 10


def test_floors(client):
    """Storeys are read from the store, not hardcoded for one demo building.

    This used to return a fixed 8 x 3.2 m stack for ``B-001`` whatever the
    scene contained, each row carrying ``confidence: 0.918``. A hardcoded
    answer cannot describe data it has not seen, and a literal is not a
    measurement.
    """
    body = client.get("/floors").json()
    assert len(body) == 8
    assert {f["building_id"] for f in body} == {"B-001"}
    assert [f["floor_number"] for f in body] == list(range(1, 9))
    assert body[0]["z_min"] == 0 and body[0]["z_max"] == 3.2
    assert "confidence" not in body[0]
    # Every storey carries a real id, so the rows are records rather than a
    # synthesised list.
    assert all(f["id"] for f in body)


def test_ulpin_search_by_identifier(client):
    ulpin = client.get("/properties").json()[0]["prototype_ulpin"]
    r = client.get(f"/ulpin/{ulpin}")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "property"
    assert body["record"]["prototype_ulpin"] == ulpin


def test_ulpin_search_by_label(client):
    r = client.get("/ulpin/Apartment%20201")
    assert r.status_code == 200
    assert r.json()["record"]["id"] == "PV-201"


def test_ulpin_search_building_returns_children(client):
    body = client.get("/ulpin/B-001").json()
    assert body["kind"] == "building"
    assert len(body["related"]["children"]) == 17


def test_ulpin_search_miss_returns_404(client):
    r = client.get("/ulpin/nope")
    assert r.status_code == 404
    assert r.json()["detail"] == "No cadastral object matches this search"


def test_ulpin_generation_is_deterministic(client):
    before = {p["id"]: p["prototype_ulpin"] for p in client.get("/properties").json()}
    client.post("/ulpin/generate")
    after = {p["id"]: p["prototype_ulpin"] for p in client.get("/properties").json()}
    assert before == after
    # Regenerating a freshly seeded scene reproduces the same identifiers.
    client.get("/demo/load")
    reseeded = {p["id"]: p["prototype_ulpin"] for p in client.get("/properties").json()}
    assert before == reseeded


def test_ulpin_generate_carries_disclaimer(client):
    body = client.post("/ulpin/generate").json()
    # 1 parcel + 17 volumes carry an identifier; the seed already issued them,
    # so this call newly assigns none.
    assert body["generated"] == 18
    assert body["assigned"] == 0
    assert "not an official ULPIN" in body["label"]


def test_route_precedence_ulpin_search_before_generate(client):
    """GET /ulpin/generate is a search for the literal string, not a write."""
    r = client.get("/ulpin/generate")
    assert r.status_code == 404


def test_processing_adapters_are_deterministic(client):
    """The adapters report store counts and nothing else.

    They used to return ``confidence`` 0.974 / 0.918 / 0.991 and a ``model``
    key naming a "deterministic footprint inference adapter" -- a model that
    does not exist. No inference runs here, so there is no model to name and
    no accuracy to report. The counts are real; the score was a literal.
    """
    for stage, count_key, expected in (
        ("building-extraction", "building_count", 1),
        ("floor-segmentation", "storey_count", 8),
        ("vertical-delineation", "volume_count", 17),
    ):
        body = client.post(f"/processing/{stage}").json()
        assert body["stage"] == stage
        assert body[count_key] == expected, body
        assert body["adapter"] is True
        assert "confidence" not in body, body
        assert "model" not in body, "no model is loaded, so none may be named"
        assert "store count" in body["note"].lower()


def test_import_registers_source_without_creating_records(client):
    payload = json.dumps(
        {"type": "FeatureCollection", "features": [{"id": 1}, {"id": 2}]}
    ).encode()
    r = client.post("/import/geojson", files={"file": ("a.geojson", payload, "application/json")})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["features"] == 2
    # Scene is untouched by an import.
    assert len(client.get("/properties").json()) == 17
    assert len(client.get("/data-sources").json()) == 2  # DS-001 + the new one


def test_import_point_cloud_is_inspected_not_just_registered(client, point_cloud_upload):
    """LAS/LAZ/PLY now go through the real ingestion pipeline."""
    for ext, expected_format in (("las", "LAS"), ("laz", "LAZ"), ("ply", "PLY")):
        r = client.post(
            "/import/source",
            files={"file": (f"scan.{ext}", point_cloud_upload(ext), "application/octet-stream")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["accepted"] is True
        assert body["format"] == expected_format
        assert body["point_count"] == 4
        assert body["crs"] == "EPSG:32643"
        assert len(body["sha256"]) == 64
        if expected_format == "PLY":
            # PLY declares no extent, so bounds and density are undefined rather
            # than invented.
            assert body["bounds"] is None
            assert body["density_points_per_m2"] is None
        else:
            assert body["density_points_per_m2"] is not None
            assert body["display_bounds"]["crs"] == "EPSG:4326"
        # Metadata extraction ran; the extraction stages are declared as not
        # implemented rather than silently omitted.
        statuses = {j["job_type"]: j["status"] for j in body["jobs"]}
        assert statuses["METADATA_EXTRACTION"] == "COMPLETED"
        # Both downstream stages are implemented but not yet asked for, so they
        # are PENDING: available, not missing.
        assert statuses["BUILDING_EXTRACTION"] == "PENDING"
        assert statuses["FLOOR_SEGMENTATION"] == "PENDING"
    # Ingesting a point cloud still creates no cadastral records.
    assert len(client.get("/properties").json()) == 17


def test_import_corrupt_point_cloud_is_rejected(client):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", b"definitely not a point cloud", "application/octet-stream")},
    )
    assert r.status_code == 422
    # A rejected ingest must not register a source.
    assert all(
        s.get("metadata", {}).get("point_cloud") is not True
        for s in client.get("/data-sources").json()
    )


def test_point_cloud_endpoints_expose_what_the_ui_needs(client, point_cloud_upload):
    client.post(
        "/import/source",
        files={"file": ("scan.las", point_cloud_upload("las"), "application/octet-stream")},
    )
    clouds = client.get("/point-clouds").json()
    assert len(clouds) == 1
    row = clouds[0]
    # file, type, CRS, point count, bounds, acquisition, status
    assert row["filename"] == "scan.las"
    assert row["type"] == "Point cloud"
    assert row["crs"] == "EPSG:32643"
    assert row["point_count"] == 4
    assert row["bounds"]["crs"] == "EPSG:4326"
    assert row["acquisition"]["generating_software"]
    # Only metadata extraction ran, so the headline status says so.
    assert row["status"] == "METADATA_ONLY"

    detail = client.get(f"/point-clouds/{row['source_id']}").json()
    assert len(detail["jobs"]) == 3

    jobs = client.get("/processing-jobs").json()
    assert len(jobs) == 3


def test_import_registers_non_point_cloud_formats_without_processing(client):
    """CSV and plan JSON stay register-only; only point clouds are inspected."""
    r = client.post(
        "/import/source",
        files={"file": ("table.csv", b"x,y\n1,2", "text/csv")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source_type"] == "CSV"
    assert body["source_crs"] == "UNKNOWN"
    assert "point_count" not in body
    assert client.get("/point-clouds").json() == []


def test_import_rejects_unsupported_extension(client):
    r = client.post(
        "/import/source", files={"file": ("a.exe", b"x", "application/octet-stream")}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Supported inputs: GeoJSON, JSON, CSV, LAS/LAZ, or PLY"


def test_import_rejects_invalid_json(client):
    r = client.post(
        "/import/geojson", files={"file": ("a.geojson", b"{not json", "application/json")}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid JSON"


def test_import_geojson_rejects_wrong_extension(client):
    r = client.post(
        "/import/geojson", files={"file": ("a.csv", b"x", "text/csv")}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Upload GeoJSON or JSON"


def test_openapi_surface_is_unchanged(client):
    spec = client.get("/openapi.json").json()
    paths = {
        p: sorted(m.upper() for m in ops if m in ("get", "post", "put", "patch", "delete"))
        for p, ops in spec["paths"].items()
    }
    # 21 core routes plus point-cloud, processing-job, extraction, storey,
    # property-volume, rule-engine, review-workflow, audit, change and provenance
    # views.
    assert len(paths) == 57
    assert paths["/point-clouds"] == ["GET"]
    assert paths["/point-clouds/{source_id}"] == ["GET"]
    assert paths["/point-clouds/{source_id}/extract"] == ["POST"]
    assert paths["/point-clouds/{source_id}/segment-floors"] == ["POST"]
    assert paths["/point-clouds/{source_id}/property-volumes"] == ["POST"]
    assert paths["/extracted-buildings"] == ["GET"]
    assert paths["/extracted-floors"] == ["GET"]
    assert paths["/generated-property-volumes"] == ["GET"]
    assert paths["/processing-jobs"] == ["GET"]
    # The original two validation routes, unchanged.
    assert paths["/validation/run"] == ["POST"]
    assert paths["/validation/issues"] == ["GET"]
    # The rule engine's own surface.
    assert paths["/validation/rules"] == ["GET"]
    assert paths["/validation/rules/{rule_id}/run"] == ["POST"]
    assert paths["/validation/summary"] == ["GET"]
    # Human verification and audit history. Read-only for audit: there is
    # deliberately no route that writes, edits or deletes an audit event.
    assert paths["/reviews"] == ["GET", "POST"]
    assert paths["/reviews/{ident}"] == ["GET"]
    assert paths["/reviews/{ident}/decisions"] == ["GET"]
    for action in ("approve", "reject", "request-resurvey", "mark-expected", "assign"):
        assert paths[f"/reviews/{{ident}}/{action}"] == ["POST"], action
    assert paths["/reviews/issues/{issue_id}/close"] == ["POST"]
    assert paths["/reviews/issues/{issue_id}/reopen"] == ["POST"]
    assert paths["/audit/events"] == ["GET"]
    assert paths["/audit/history/{object_id}"] == ["GET"]
    assert paths["/audit/actions"] == ["GET"]
    # Change detection. Read-only apart from the comparison itself, which
    # records what it observed; no route edits a cadastral record.
    assert paths["/changes"] == ["GET"]
    assert paths["/changes/report"] == ["GET"]
    assert paths["/changes/compare"] == ["POST"]
    assert paths["/changes/compare/repository"] == ["GET"]
    # Provenance. Read-only: there is deliberately no route that writes, edits
    # or deletes a link, since a rewritten provenance record is a false record.
    assert paths["/provenance/stages"] == ["GET"]
    assert paths["/provenance/{object_id}"] == ["GET"]
    assert paths["/provenance/{object_id}/lineage"] == ["GET"]
    assert paths["/provenance/{object_id}/lineage/stages"] == ["GET"]
    assert paths["/provenance/{object_id}/sources"] == ["GET"]
    assert paths["/provenance/{object_id}/processing"] == ["GET"]
    assert paths["/provenance/source/{source_id}/derived"] == ["GET"]
    assert "/ulpin/{ulpin}" in paths
    assert paths["/ulpin/generate"] == ["POST"]
    assert paths["/health"] == ["GET"]
    assert paths["/demo/load"] == ["GET"]
    assert paths["/demo/load"] == ["GET"]
    assert spec["info"]["title"] == "V-CAD API"
    assert spec["info"]["version"] == "0.1.0"
    assert "not official Government of India ULPINs" in spec["info"]["description"]
