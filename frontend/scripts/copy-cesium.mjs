import { cp, mkdir, rm } from "node:fs/promises";
const from = "node_modules/cesium/Build/Cesium";
const to = "public/cesium";
await rm(to, { recursive: true, force: true });
await mkdir("public", { recursive: true });
await cp(from, to, { recursive: true });
console.log("Cesium runtime assets copied to public/cesium");
