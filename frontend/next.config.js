/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone", // Necesario para que el Dockerfile funcione
};

module.exports = nextConfig;