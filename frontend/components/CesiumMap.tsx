"use client";
import { useEffect, useRef, useState } from "react";

type AnyRecord = Record<string, any>;
type Overlay = { geometry:AnyRecord; zMin?:number|null; zMax?:number|null; color:string; label?:string };
type Props = { parcels:AnyRecord[]; buildings:AnyRecord[]; properties:AnyRecord[]; infrastructure:AnyRecord[]; selected?:AnyRecord|null; focus?:AnyRecord|null; floor:number|"all"; underground:boolean; onPick:(item:AnyRecord)=>void; overlay?:Overlay[]; };

const isPosition = (value:any):value is number[] =>
  Array.isArray(value) &&
  value.length >= 2 &&
  typeof value[0] === "number" &&
  Number.isFinite(value[0]) &&
  value[0] >= -180 &&
  value[0] <= 180 &&
  typeof value[1] === "number" &&
  Number.isFinite(value[1]) &&
  value[1] >= -90 &&
  value[1] <= 90;

const coordinateArray = (value:any):value is number[] =>
  Array.isArray(value) &&
  value.length >= 4 &&
  value.length % 2 === 0 &&
  value.every((coordinate:number,index:number) =>
    typeof coordinate === "number" &&
    Number.isFinite(coordinate) &&
    (index % 2 === 0 ? coordinate >= -180 && coordinate <= 180 : coordinate >= -90 && coordinate <= 90)
  );

const flatRing = (value:any):number[]|null => {
  if (!Array.isArray(value)) return null;
  const flat:number[] = [];
  for (const position of value) {
    if (!isPosition(position)) return null;
    flat.push(position[0],position[1]);
  }
  return coordinateArray(flat) && flat.length >= 6 ? flat : null;
};

/** Returns each polygon as flat exterior and interior rings in GeoJSON order. */
const polygons = (geo:any):number[][][] => {
  if (!geo || typeof geo !== "object") return [];
  if (geo.type === "GeometryCollection") {
    return Array.isArray(geo.geometries)
      ? geo.geometries.flatMap((part:any) => polygons(part))
      : [];
  }

  const asPolygon = (coordinates:any):number[][]|null => {
    if (!Array.isArray(coordinates) || !coordinates.length) return null;
    const rings = coordinates.map(flatRing);
    if (rings.some((ring:number[]|null) => ring === null)) return null;
    return rings as number[][];
  };

  if (geo.type === "Polygon") {
    const polygon = asPolygon(geo.coordinates);
    return polygon ? [polygon] : [];
  }
  if (geo.type === "MultiPolygon") {
    if (!Array.isArray(geo.coordinates)) return [];
    const result = geo.coordinates.map(asPolygon);
    return result.some((polygon:number[][]|null) => polygon === null)
      ? []
      : result as number[][][];
  }
  return [];
};

const coords = (geo:any):number[] => polygons(geo)[0]?.[0] ?? [];

const cartesianPositions = (Cesium:any, coordinates:any):any[] =>
  coordinateArray(coordinates)
    ? Cesium.Cartesian3.fromDegreesArray(coordinates)
    : [];

const cartesianPositionsWithHeights = (Cesium:any, coordinates:any):any[] => {
  if (
    !Array.isArray(coordinates) ||
    coordinates.length < 6 ||
    coordinates.length % 3 !== 0 ||
    !coordinates.every((coordinate:number) => typeof coordinate === "number" && Number.isFinite(coordinate))
  ) return [];
  for (let i = 0; i < coordinates.length; i += 3) {
    if (
      coordinates[i] < -180 || coordinates[i] > 180 ||
      coordinates[i + 1] < -90 || coordinates[i + 1] > 90
    ) return [];
  }
  return Cesium.Cartesian3.fromDegreesArrayHeights(coordinates);
};

const colour = (r:AnyRecord) =>
  r.property_type === "BASEMENT_PARKING" ? "#5c82a8" :
  r.type ? "#d89936" :
  r.status === "HUMAN REVIEW REQUIRED" ? "#f1a61c" :
  "#3cb5a5";

export default function CesiumMap({
  parcels,
  buildings,
  properties,
  infrastructure,
  selected,
  focus,
  floor,
  underground,
  onPick,
  overlay
}:Props) {
  const mount = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<any>(null);
  const cesiumRef = useRef<any>(null);
  const entityRef = useRef<Map<string,any>>(new Map());

  const onPickRef = useRef(onPick);
  onPickRef.current = onPick;

  const [viewerReady, setViewerReady] = useState(false);

  const fittedDataRef = useRef<{
    parcels:AnyRecord[];
    buildings:AnyRecord[];
    properties:AnyRecord[];
    infrastructure:AnyRecord[];
  } | null>(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      const Cesium = await import("cesium");
      if (cancelled || !mount.current) return;

      (window as any).CESIUM_BASE_URL = "/cesium";

      const viewer = new Cesium.Viewer(mount.current, {
        animation:false,
        timeline:false,
        geocoder:false,
        homeButton:false,
        sceneModePicker:false,
        baseLayerPicker:false,
        navigationHelpButton:false,
        infoBox:false,
        selectionIndicator:false,
        baseLayer:false,
        fullscreenButton:false
      });

      viewerRef.current = viewer;
      cesiumRef.current = Cesium;

      viewer.scene.backgroundColor =
        Cesium.Color.fromCssColorString("#07151d");

      viewer.scene.globe.baseColor =
        Cesium.Color.fromCssColorString("#112e32");

      viewer.scene.globe.showGroundAtmosphere = false;

      const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);

      handler.setInputAction(
        (movement:any) => {
          const picked = viewer.scene.pick(movement.position);
          if (picked?.id?.vcadRecord) {
            onPickRef.current(picked.id.vcadRecord);
          }
        },
        Cesium.ScreenSpaceEventType.LEFT_CLICK
      );

      setViewerReady(true);
    })();

    return () => {
      cancelled = true;

      if (
        viewerRef.current &&
        !viewerRef.current.isDestroyed()
      ) {
        viewerRef.current.destroy();
      }

      viewerRef.current = null;
      cesiumRef.current = null;
      setViewerReady(false);
    };
  }, []);

  useEffect(() => {
    const viewer = viewerRef.current;
    const Cesium = cesiumRef.current;

    if (!viewer || !Cesium || !viewerReady) return;

    viewer.entities.removeAll();
    entityRef.current.clear();

    const scenePoints:any[] = [];

    const add = (
      r:AnyRecord,
      geo:any,
      zmin:number,
      zmax:number,
      color:string,
      outline=true
    ) => {
      const ring = coords(geo);
      if (!coordinateArray(ring)) return;

      const low = Number.isFinite(zmin) ? zmin : 0;
      const high = Number.isFinite(zmax) ? zmax : low;

      const heights:number[] = [];

      for (let i = 0; i < ring.length; i += 2) {
        heights.push(
          ring[i],
          ring[i + 1],
          low,
          ring[i],
          ring[i + 1],
          high
        );
      }

      if (heights.length) {
        for (const point of cartesianPositionsWithHeights(Cesium,heights)) {
          scenePoints.push(point);
        }
      }

      const positions = cartesianPositions(Cesium,ring);
      if (!positions.length) return;
      const e = viewer.entities.add({
        name:r.unit_label || r.building_id || r.parcel_id || r.type,
        vcadRecord:r,
        polygon:{
          hierarchy:positions,
          height:low,
          extrudedHeight:high,
          material:Cesium.Color
            .fromCssColorString(color)
            .withAlpha(r.id === selected?.id ? 0.92 : 0.62),
          outline,
          outlineColor:Cesium.Color
            .fromCssColorString(
              r.id === selected?.id ? "#fff1a3" : "#a4dad4"
            ),
          outlineWidth:r.id === selected?.id ? 3 : 1,
          perPositionHeight:false
        }
      });

      entityRef.current.set(r.id,e);
    };

    parcels.forEach(p =>
      add(p,p.geometry,0,.15,"#456a57")
    );

    buildings.forEach(b =>
      add(b,b.footprint,0,b.height,"#63808c",false)
    );

    properties
      .filter(
        p =>
          floor === "all" ||
          p.floor_number === floor ||
          (floor === -1 && p.floor_number === -1)
      )
      .forEach(p =>
        add(
          p,
          p.geometry_3d,
          p.z_min,
          p.z_max,
          colour(p)
        )
      );

    if (underground) {
      infrastructure.forEach(i =>
        add(
          i,
          i.geometry_3d,
          i.z_min,
          i.z_max,
          colour(i)
        )
      );
    }

    // Preserve existing verification overlays.
    (overlay || []).forEach((layer:Overlay,index:number) => {
      const parts = polygons(layer.geometry);
      if (!parts.length) {
        if (process.env.NODE_ENV !== "production") {
          console.warn(`Skipping malformed Cesium overlay${layer.label ? ` "${layer.label}"` : ""}.`);
        }
        return;
      }

      const zmin =
        typeof layer.zMin === "number"
          ? layer.zMin
          : 0;

      const zmax =
        typeof layer.zMax === "number"
          ? layer.zMax
          : zmin + 1;

      parts.forEach((polygon:number[][],part:number) => {
        const positions = cartesianPositions(Cesium,polygon[0]);
        if (!positions.length) return;
        const holes = polygon.slice(1)
          .map((ring) => cartesianPositions(Cesium,ring))
          .filter((ring) => ring.length > 0)
          .map((ring) => new Cesium.PolygonHierarchy(ring));
        const e = viewer.entities.add({
          name:layer.label,
          polygon:{
            hierarchy:new Cesium.PolygonHierarchy(positions,holes),
            height:zmin,
            extrudedHeight:zmax,
            material:Cesium.Color
              .fromCssColorString(layer.color)
              .withAlpha(0.55),
            outline:true,
            outlineColor:Cesium.Color
              .fromCssColorString(layer.color),
            outlineWidth:2,
            perPositionHeight:false
          }
        });

        entityRef.current.set(
          `overlay-${index}-${part}`,
          e
        );
      });
    });

    if (!fittedDataRef.current && scenePoints.length) {
      const bounds =
        Cesium.BoundingSphere.fromPoints(scenePoints);

      viewer.camera.flyToBoundingSphere(
        bounds,
        {
          duration:0.8,
          offset:new Cesium.HeadingPitchRange(
            Cesium.Math.toRadians(20),
            Cesium.Math.toRadians(-35),
            Math.max(bounds.radius * 3,100)
          )
        }
      );

      fittedDataRef.current = {
        parcels,
        buildings,
        properties,
        infrastructure
      };
    }
  }, [
    viewerReady,
    parcels,
    buildings,
    properties,
    infrastructure,
    selected,
    floor,
    underground,
    overlay
  ]);

  useEffect(() => {
    const viewer = viewerRef.current;
    const Cesium = cesiumRef.current;

    if (!viewer || !Cesium || !viewerReady || !focus) return;

    const geo =
      focus.geometry_3d ||
      focus.geometry ||
      focus.footprint;

    if (!geo) return;

    const ring = coords(geo);

    const positions = cartesianPositions(Cesium,ring);
    if (!positions.length) return;

    viewer.camera.flyTo({
      destination:
        Cesium.BoundingSphere.fromPoints(
          positions
        ).center,
      duration:.8,
      offset:new Cesium.HeadingPitchRange(
        0,
        Cesium.Math.toRadians(-45),
        120
      )
    });
  }, [viewerReady,focus]);

  return (
    <div
      ref={mount}
      className="h-full w-full"
      aria-label="Interactive Cesium 3D cadastral map"
    />
  );
}