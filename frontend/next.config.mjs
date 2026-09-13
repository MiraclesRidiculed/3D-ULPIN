/** @type {import('next').NextConfig} */
const nextConfig = {
  env: { CESIUM_BASE_URL: "/cesium" },
  webpack: (config) => {
    config.output.sourcePrefix = "";
    config.amd = { toUrlUndefined: true };
    return config;
  },
};
export default nextConfig;
