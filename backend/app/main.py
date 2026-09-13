from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from shapely.geometry import Polygon, box
from shapely.ops import unary_union
from shapely.validation import explain_validity

app = FastAPI(
    title="V-CAD API",
    version="0.1.0",
    description="Prototype 3D ULPIN and volumetric cadastral API. Identifiers are not official Government of India ULPINs.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

Severity = Literal["INFO", "WARNING", "CRITICAL"]

class Geometry(BaseModel):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[list[float]]]

class Parcel(BaseModel):
    id: str; parcel_id: str; prototype_ulpin: str | None = None; geometry: Geometry
    area: float; land_use: str; survey_reference: str; created_at: str; updated_at: str; version: int = 1

class Building(BaseModel):
    id: str; building_id: str; parcel_id: str; footprint: Geometry; height: float; floor_count: int
    confidence: float; geometry_3d: dict[str, Any]; created_at: str; updated_at: str; version: int = 1

class PropertyVolume(BaseModel):
    id: str; prototype_ulpin: str | None = None; parent_parcel_id: str; building_id: str | None = None
    property_type: str; floor_number: int | None = None; unit_label: str | None = None
    z_min: float; z_max: float; geometry_3d: Geometry; volume_m3: float; area_m2: float
    geometry_hash: str; status: str; confidence: float; created_at: str; updated_at: str; version: int = 1

class Infrastructure(BaseModel):
    id: str; type: str; geometry_3d: Geometry; z_min: float; z_max: float; owner: str; reference: str
    created_at: str; updated_at: str; version: int = 1

class ValidationIssue(BaseModel):
    id: str; object_a: str; object_b: str | None = None; issue_type: str; severity: Severity
    overlap_volume: float = 0; gap_m: float | None = None; description: str; geometry: Geometry | None = None
    status: str = "OPEN"; location: str

class GenerateRequest(BaseModel):
    parent_parcel_id: str | None = None

class SearchResult(BaseModel):
    kind: str; record: dict[str, Any]; related: dict[str, list[str]] = Field(default_factory=dict)

DB: dict[str, list[dict[str, Any]]] = {"parcels": [], "buildings": [], "properties": [], "infrastructure": [], "issues": [], "sources": []}

ORIGIN_LON, ORIGIN_LAT = 77.2089, 28.6131
def now() -> str: return datetime.now(timezone.utc).isoformat()
def xy_to_ll(x: float, y: float) -> list[float]: return [round(ORIGIN_LON + x / 98000, 7), round(ORIGIN_LAT + y / 111000, 7)]
def poly_geo(p: Polygon) -> dict[str, Any]: return {"type": "Polygon", "coordinates": [[xy_to_ll(x, y) for x, y in p.exterior.coords]]}
def geo_poly(geo: dict[str, Any]) -> Polygon:
    return Polygon([((pt[0] - ORIGIN_LON) * 98000, (pt[1] - ORIGIN_LAT) * 111000) for pt in geo["coordinates"][0]])
def geometry_hash(p: Polygon, zmin: float, zmax: float) -> str:
    rounded = [(round(x, 3), round(y, 3)) for x, y in p.exterior.coords]
    return hashlib.sha256(json.dumps({"ring": rounded, "z": [round(zmin, 3), round(zmax, 3)]}, sort_keys=True).encode()).hexdigest()[:12].upper()
def spatial_code(p: Polygon) -> str:
    c = p.centroid
    return f"{int((c.x + 1000) * 10):05d}{int((c.y + 1000) * 10):05d}"
def volume_intersection(a: dict[str, Any], b: dict[str, Any]) -> tuple[Polygon, float]:
    overlap = geo_poly(a["geometry_3d"]).intersection(geo_poly(b["geometry_3d"]))
    z = max(0, min(a["z_max"], b["z_max"]) - max(a["z_min"], b["z_min"]))
    return overlap, overlap.area * z

def make_volume(*, ident: str, parcel: str, building: str | None, prop_type: str, floor: int | None,
                label: str, footprint: Polygon, zmin: float, zmax: float, confidence: float, status: str = "MAPPED") -> dict[str, Any]:
    timestamp = now(); h = geometry_hash(footprint, zmin, zmax)
    return {"id": ident, "prototype_ulpin": None, "parent_parcel_id": parcel, "building_id": building,
            "property_type": prop_type, "floor_number": floor, "unit_label": label, "z_min": zmin, "z_max": zmax,
            "geometry_3d": poly_geo(footprint), "volume_m3": round(footprint.area * (zmax-zmin), 2),
            "area_m2": round(footprint.area, 2), "geometry_hash": h, "status": status, "confidence": confidence,
            "created_at": timestamp, "updated_at": timestamp, "version": 1}

def seed_demo() -> dict[str, Any]:
    global DB
    timestamp = now(); parcel_poly = box(0, 0, 80, 52); building_poly = box(10, 8, 70, 44)
    parcel = {"id": "parcel-001", "parcel_id": "P-001", "prototype_ulpin": None, "geometry": poly_geo(parcel_poly),
              "area": parcel_poly.area, "land_use": "Mixed residential", "survey_reference": "Demo Ward 17 / Sheet 04",
              "created_at": timestamp, "updated_at": timestamp, "version": 1}
    building = {"id": "building-001", "building_id": "B-001", "parcel_id": "P-001", "footprint": poly_geo(building_poly),
                "height": 25.6, "floor_count": 8, "confidence": 0.974, "geometry_3d": {"z_min": 0, "z_max": 25.6, "footprint": poly_geo(building_poly)},
                "created_at": timestamp, "updated_at": timestamp, "version": 1}
    props: list[dict[str, Any]] = []
    props.append(make_volume(ident="PV-B001", parcel="P-001", building="B-001", prop_type="BASEMENT_PARKING", floor=-1, label="Basement Parking", footprint=building_poly, zmin=-3.2, zmax=0, confidence=.918))
    for floor in range(1, 9):
        zmin, zmax = (floor-1)*3.2, floor*3.2
        # A deliberate floor 2 overlap and a floor 5 parcel-boundary encroachment.
        left = box(10, 8, 40, 44)
        right = box(40, 8, 70, 44)
        if floor == 2: right = box(35, 8, 70, 44)
        if floor == 5: right = box(40, 8, 86, 44)
        props.extend([
            make_volume(ident=f"PV-{floor}01", parcel="P-001", building="B-001", prop_type="APARTMENT", floor=floor, label=f"Apartment {floor}01", footprint=left, zmin=zmin, zmax=zmax, confidence=.918),
            make_volume(ident=f"PV-{floor}02", parcel="P-001", building="B-001", prop_type="APARTMENT", floor=floor, label=f"Apartment {floor}02", footprint=right, zmin=zmin, zmax=zmax, confidence=.918, status="HUMAN REVIEW REQUIRED" if floor == 5 else "MAPPED")])
    utility_poly = box(18, 21, 62, 25)
    utility = {"id":"INF-U-001", "type":"UNDERGROUND_UTILITY_CORRIDOR", "geometry_3d":poly_geo(utility_poly), "z_min":-2.2, "z_max":0.0,
               "owner":"Demo Municipal Utility Cell", "reference":"UG-UTIL-17", "created_at":timestamp, "updated_at":timestamp, "version":1}
    DB.clear(); DB.update({"parcels":[parcel], "buildings":[building], "properties":props, "infrastructure":[utility], "issues":[],
          "sources":[{"id":"DS-001","source_type":"Synthetic GeoJSON","filename":"demo_city.geojson","crs":"EPSG:4326","acquisition_date":"2026-09-01","metadata":{"generated":True}}]}
    )
    generate_ulpins(); validate()
    return {"message":"Demo City loaded", "properties":len(props), "issues":len(DB["issues"])}

def generate_ulpins() -> dict[str, Any]:
    records = DB["parcels"] + DB["properties"]
    for record in records:
        if "geometry" in record: p, zmin, zmax, prefix = geo_poly(record["geometry"]), 0, 0, "LP"
        else: p, zmin, zmax, prefix = geo_poly(record["geometry_3d"]), record["z_min"], record["z_max"], "VP"
        h = geometry_hash(p, zmin, zmax)
        parent = record.get("parent_parcel_id", record.get("parcel_id", "ROOT")).replace("-", "")
        unit = (record.get("unit_label") or record.get("parcel_id") or "LAND").replace(" ", "")[-8:].upper()
        record["geometry_hash"] = h
        record["prototype_ulpin"] = f"VC-{prefix}-{parent}-{unit}-{spatial_code(p)}-{h[:6]}"
        record["updated_at"] = now()
    return {"generated": len(records), "label":"Prototype 3D ULPIN — deterministic prototype identifier, not an official ULPIN."}

def issue(ident: str, a: str, b: str | None, typ: str, severity: Severity, description: str, geom: Polygon | None, location: str, overlap: float = 0, gap: float | None = None) -> None:
    DB["issues"].append({"id":ident, "object_a":a, "object_b":b, "issue_type":typ, "severity":severity,
        "overlap_volume":round(overlap,2), "gap_m":gap, "description":description, "geometry":poly_geo(geom) if geom and not geom.is_empty else None,
        "status":"OPEN", "location":location})

def validate() -> dict[str, Any]:
    DB["issues"] = []
    parcel = DB["parcels"][0]; parent_poly = geo_poly(parcel["geometry"])
    props = DB["properties"]
    # invalidity and parcel containment
    for p in props:
        shape = geo_poly(p["geometry_3d"])
        if not shape.is_valid: issue(f"VAL-INV-{p['id']}",p["id"],None,"INVALID_GEOMETRY","CRITICAL",explain_validity(shape),shape,p["unit_label"])
        outside = shape.difference(parent_poly)
        if not outside.is_empty and outside.area > .01:
            issue(f"VAL-OUT-{p['id']}",p["id"],"P-001","OUTSIDE_PARENT_PARCEL","WARNING",f"{p['unit_label']} extends {outside.area:.1f} m² outside parent parcel P-001.",outside,p["unit_label"])
    # overlaps only within like vertical bands, avoiding containment hierarchy
    for i, a in enumerate(props):
        for b in props[i+1:]:
            if a["floor_number"] != b["floor_number"]: continue
            shape, vol = volume_intersection(a,b)
            if vol > .01:
                issue(f"VAL-OVR-{a['id']}-{b['id']}",a["id"],b["id"],"VERTICAL_VOLUME_OVERLAP","CRITICAL",
                      f"{a['unit_label']} and {b['unit_label']} occupy the same 3D space ({vol:.1f} m³).",shape,f"B-001 / Floor {a['floor_number']}",vol)
    # Floor-stack checks: Z order, explicit gaps, disconnected plan stacks, and duplicate ULPIN geometries.
    by_floor: dict[int, list[dict[str, Any]]] = {}
    for p in props:
        if p["floor_number"] is not None: by_floor.setdefault(p["floor_number"], []).append(p)
        if p["z_min"] >= p["z_max"]:
            issue(f"VAL-Z-{p['id']}",p["id"],None,"FLOOR_Z_ORDER_INCONSISTENCY","CRITICAL",f"{p['unit_label']} has z_min greater than or equal to z_max.",geo_poly(p["geometry_3d"]),p["unit_label"])
    bands = sorted(((n, min(x["z_min"] for x in ps), max(x["z_max"] for x in ps), unary_union([geo_poly(x["geometry_3d"]) for x in ps])) for n, ps in by_floor.items()), key=lambda x:x[1])
    for (n1, _, top, shape1), (n2, bottom, _, shape2) in zip(bands, bands[1:]):
        if bottom - top > .05:
            issue(f"VAL-GAP-{n1}-{n2}",f"FLOOR-{n1}",f"FLOOR-{n2}","EXPECTED_ADJACENT_FLOOR_GAP","WARNING",f"A {bottom-top:.2f} m vertical gap separates expected adjacent floors {n1} and {n2}.",None,"Building B-001",gap=round(bottom-top,2))
        if shape1.intersection(shape2).area < .01:
            issue(f"VAL-DISC-{n1}-{n2}",f"FLOOR-{n1}",f"FLOOR-{n2}","DISCONNECTED_FLOOR_STACK","WARNING",f"Floor {n2} has no plan-area connection to floor {n1}.",None,"Building B-001")
    seen: dict[str, dict[str, Any]] = {}
    for p in props:
        key = p.get("prototype_ulpin")
        if key in seen:
            other = seen[key]; shape, vol = volume_intersection(p, other)
            issue(f"VAL-DUP-{p['id']}",p["id"],other["id"],"DUPLICATE_ULPIN_GEOMETRY","CRITICAL",f"Prototype 3D ULPIN {key} is assigned to two property records.",shape,p["unit_label"],vol)
        elif key: seen[key] = p
    infra = DB["infrastructure"][0]
    infra_proxy = {"geometry_3d":infra["geometry_3d"], "z_min":infra["z_min"], "z_max":infra["z_max"]}
    for p in props:
        shape, vol = volume_intersection(p, infra_proxy)
        if vol > .01:
            issue(f"VAL-INF-{p['id']}",p["id"],infra["id"],"UNDERGROUND_INFRASTRUCTURE_COLLISION","CRITICAL",
                  f"{infra['type'].replace('_',' ').title()} intersects {p['unit_label']} by {vol:.1f} m³.",shape,"Basement / Utility layer",vol)
    return {"issues":len(DB["issues"]), "critical":sum(x["severity"] == "CRITICAL" for x in DB["issues"]), "warning":sum(x["severity"] == "WARNING" for x in DB["issues"])}

def find(ident: str) -> SearchResult:
    q = ident.strip().lower()
    for kind, key in [("parcel","parcels"),("building","buildings"),("property","properties"),("infrastructure","infrastructure")]:
        for r in DB[key]:
            hay = " ".join(str(r.get(k,"")) for k in ["id","parcel_id","building_id","prototype_ulpin","unit_label","type"] ).lower()
            if q in hay:
                related = {"children":[p["id"] for p in DB["properties"] if p.get("building_id") == r.get("building_id")]} if kind == "building" else {}
                return SearchResult(kind=kind, record=r, related=related)
    raise HTTPException(404, "No cadastral object matches this search")

@app.on_event("startup")
def bootstrap() -> None:
    if not DB["parcels"]: seed_demo()

@app.get("/health")
def health() -> dict[str, str]: return {"status":"ok", "storage":"local deterministic demo store"}
@app.get("/demo/load")
def load_demo() -> dict[str, Any]: return seed_demo()
@app.get("/parcels", response_model=list[Parcel])
def parcels() -> list[dict[str,Any]]: return DB["parcels"]
@app.get("/parcels/{ident}")
def parcel(ident:str) -> dict[str,Any]: return find(ident).record
@app.get("/buildings", response_model=list[Building])
def buildings() -> list[dict[str,Any]]: return DB["buildings"]
@app.get("/buildings/{ident}")
def building(ident:str) -> dict[str,Any]: return find(ident).record
@app.get("/floors")
def floors() -> list[dict[str,Any]]:
    return [{"building_id":"B-001","floor_number":f,"z_min":(f-1)*3.2,"z_max":f*3.2,"confidence":.918} for f in range(1,9)]
@app.get("/properties", response_model=list[PropertyVolume])
def properties() -> list[dict[str,Any]]: return DB["properties"]
@app.get("/properties/{ident}")
def property_volume(ident:str) -> dict[str,Any]: return find(ident).record
@app.get("/infrastructure", response_model=list[Infrastructure])
def infrastructure() -> list[dict[str,Any]]: return DB["infrastructure"]
@app.get("/data-sources")
def data_sources() -> list[dict[str,Any]]: return DB["sources"]
@app.get("/ulpin/{ulpin}", response_model=SearchResult)
def ulpin(ulpin:str) -> SearchResult: return find(ulpin)
@app.post("/ulpin/generate")
def ulpin_generate(_:GenerateRequest | None = None) -> dict[str,Any]: return generate_ulpins()
@app.post("/processing/building-extraction")
def building_extraction() -> dict[str,Any]: return {"stage":"building-extraction","building_count":len(DB["buildings"]),"confidence":0.974,"model":"deterministic footprint inference adapter"}
@app.post("/processing/floor-segmentation")
def floor_segmentation() -> dict[str,Any]: return {"stage":"floor-segmentation","floor_count":8,"confidence":0.918,"model":"deterministic floor segmentation adapter"}
@app.post("/processing/vertical-delineation")
def vertical_delineation() -> dict[str,Any]: return {"stage":"vertical-delineation","volumes":len(DB["properties"]),"confidence":0.991,"model":"topology-aware vertical delineation adapter"}
@app.post("/validation/run")
def run_validation() -> dict[str,Any]: return validate()
@app.get("/validation/issues", response_model=list[ValidationIssue])
def validation_issues() -> list[dict[str,Any]]: return DB["issues"]
@app.get("/analytics/summary")
def analytics() -> dict[str,Any]:
    issues=DB["issues"]; props=DB["properties"]
    return {"parcels":len(DB["parcels"]),"buildings":len(DB["buildings"]),"property_volumes":len(props),
            "apartments":sum(p["property_type"]=="APARTMENT" for p in props),"underground_assets":len(DB["infrastructure"]),
            "validation_issues":len(issues),"critical_conflicts":sum(x["severity"]=="CRITICAL" for x in issues),
            "average_confidence":round(sum(p["confidence"] for p in props)/len(props)*100,1),"distribution": [{"level":"Utility", "count":1},{"level":"Basement", "count":1}]+[{"level":f"Floor {n}","count":2} for n in range(8,0,-1)]}
@app.post("/import/geojson")
async def import_geojson(file: UploadFile = File(...)) -> dict[str,Any]:
    if not file.filename or not file.filename.lower().endswith((".geojson", ".json")): raise HTTPException(400,"Upload GeoJSON or JSON")
    raw=await file.read()
    if len(raw)>10_000_000: raise HTTPException(413,"Maximum upload size is 10 MB")
    try: data=json.loads(raw)
    except json.JSONDecodeError: raise HTTPException(400,"Invalid JSON")
    DB["sources"].append({"id":f"DS-{len(DB['sources'])+1:03d}","source_type":"GeoJSON","filename":file.filename,"crs":"EPSG:4326","acquisition_date":now()[:10],"metadata":{"features":len(data.get('features',[]))}})
    return {"accepted":True,"features":len(data.get("features",[])),"note":"Source registered. Production adapter would persist imported features to PostGIS."}
@app.post("/import/source")
async def import_source(file: UploadFile = File(...), source_type: str = "auto") -> dict[str, Any]:
    """Register bounded demo inputs: GeoJSON, CSV, floor-plan JSON, and point-cloud placeholders."""
    if not file.filename: raise HTTPException(400, "A filename is required")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    supported = {"geojson":"GeoJSON", "json":"Floor-plan JSON", "csv":"CSV", "las":"Point cloud", "laz":"Point cloud", "ply":"Point cloud"}
    if ext not in supported: raise HTTPException(400, "Supported inputs: GeoJSON, JSON, CSV, LAS/LAZ, or PLY")
    raw = await file.read()
    if len(raw) > 10_000_000: raise HTTPException(413, "Maximum upload size is 10 MB")
    detected = supported[ext] if source_type == "auto" else source_type[:50]
    DB["sources"].append({"id":f"DS-{len(DB['sources'])+1:03d}","source_type":detected,"filename":file.filename[:255],"crs":"Unspecified","acquisition_date":now()[:10],"metadata":{"bytes":len(raw),"ingestion":"registered for normalization"}})
    return {"accepted":True,"source_type":detected,"filename":file.filename,"bytes":len(raw),"note":"Source accepted into the normalization queue. The demo city remains the active processing scene."}
