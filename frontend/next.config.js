/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone", // Necesario para que el Dockerfile funcione

  // Proxy server-side: el browser siempre le pega a /api/* en el mismo
  // origen (localhost:3000 en dev, o el dominio publico en prod, ej.
  // https://app.orbit.best/api/...). Next.js reenvia esa llamada al
  // api-gateway real usando esta URL interna, que NO viaja al navegador.
  //
  // API_GATEWAY_INTERNAL_URL es una env var de servidor (sin prefijo
  // NEXT_PUBLIC_), asi que se puede cambiar en runtime (docker-compose,
  // .env, etc.) sin tener que rebuildear la imagen del frontend.
  async rewrites() {
    const target = process.env.API_GATEWAY_INTERNAL_URL || "http://localhost:8200";
    return [
      {
        source: "/api/:path*",
        destination: `${target}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
