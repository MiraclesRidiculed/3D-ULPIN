"use client";
import { useEffect, useRef } from "react";

type AnyRecord = Record<string, any>;
type Props = { parcels:AnyRecord[]; buildings:AnyRecord[]; properties:AnyRecord[]; infrastructure:AnyRecord[]; selected?:AnyRecord|null; focus?:AnyRecord|null; floor:number|"all"; underground:boolean; onPick:(item:AnyRecord)=>void; };

const coords = (geo:any) => geo?.coordinates?.[0]?.flatMap((p:number[]) => [p[0],p[1]]) || [];
const colour = (r:AnyRecord) => r.property_type === "BASEMENT_PARKING" ? "#5c82a8" : r.type ? "#d89936" : r.status === "HUMAN REVIEW REQUIRED" ? "#f1a61c" : "#3cb5a5";

export default function CesiumMap({parcels, buildings, properties, infrastructure, selected, focus, floor, underground, onPick}:Props) {
  const mount = useRef<HTMLDivElement>(null); const viewerRef = useRef<any>(null); const cesiumRef = useRef<any>(null); const entityRef = useRef<Map<string,any>>(new Map());
  useEffect(() => {
    let cancelled=false;
    (async () => {
      const Cesium = await import("cesium"); if (cancelled || !mount.current) return;
      (window as any).CESIUM_BASE_URL = "/cesium";
      const viewer = new Cesium.Viewer(mount.current, { animation:false, timeline:false, geocoder:false, homeButton:false, sceneModePicker:false, baseLayerPicker:false, navigationHelpButton:false, infoBox:false, selectionIndicator:false, baseLayer:false, fullscreenButton:false });
      viewerRef.current=viewer; cesiumRef.current=Cesium;
      viewer.scene.backgroundColor = Cesium.Color.fromCssColorString("#07151d");
      viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString("#112e32");
      viewer.scene.globe.showGroundAtmosphere = false;
      viewer.camera.flyTo({destination:Cesium.Cartesian3.fromDegrees(77.20925,28.61335,360),orientation:{heading:Cesium.Math.toRadians(25),pitch:Cesium.Math.toRadians(-42),roll:0},duration:0});
      const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
      handler.setInputAction((movement:any) => { const picked=viewer.scene.pick(movement.position); if (picked?.id?.vcadRecord) onPick(picked.id.vcadRecord); }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
    })();
    return () => { cancelled=true; if(viewerRef.current && !viewerRef.current.isDestroyed()) viewerRef.current.destroy(); viewerRef.current=null; };
  }, [onPick]);
  useEffect(() => {
    const viewer=viewerRef.current, Cesium=cesiumRef.current; if(!viewer || !Cesium) return;
    viewer.entities.removeAll(); entityRef.current.clear();
    const add=(r:AnyRecord, geo:any, zmin:number, zmax:number, color:string, outline=true) => {
      const e=viewer.entities.add({name:r.unit_label || r.building_id || r.parcel_id || r.type,vcadRecord:r,polygon:{hierarchy:Cesium.Cartesian3.fromDegreesArray(coords(geo)),height:zmin,extrudedHeight:zmax,material:Cesium.Color.fromCssColorString(color).withAlpha(r.id===selected?.id?0.92:0.62),outline,outlineColor:Cesium.Color.fromCssColorString(r.id===selected?.id?"#fff1a3":"#a4dad4"),outlineWidth:r.id===selected?.id?3:1,perPositionHeight:false}}); entityRef.current.set(r.id,e);
    };
    parcels.forEach(p=>add(p,p.geometry,0,.15,"#456a57"));
    buildings.forEach(b=>add(b,b.footprint,0,b.height,"#63808c",false));
    properties.filter(p=>floor==="all" || p.floor_number===floor || (floor===-1 && p.floor_number===-1)).forEach(p=>add(p,p.geometry_3d,p.z_min,p.z_max,colour(p)));
    if(underground) infrastructure.forEach(i=>add(i,i.geometry_3d,i.z_min,i.z_max,colour(i)));
  },[parcels,buildings,properties,infrastructure,selected,floor,underground]);
  useEffect(() => { const viewer=viewerRef.current, Cesium=cesiumRef.current; if(!viewer || !Cesium || !focus) return; const geo=focus.geometry_3d || focus.geometry || focus.footprint; if(!geo) return; viewer.camera.flyTo({destination:Cesium.BoundingSphere.fromPoints(Cesium.Cartesian3.fromDegreesArray(coords(geo))).center, duration:.8, offset:new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-45), 120)}); },[focus]);
  return <div ref={mount} className="h-full w-full" aria-label="Interactive Cesium 3D cadastral map" />;
}
