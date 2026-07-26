/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone", // Necesario para que el Dockerfile funcione

  // NOTA: el proxy hacia el api-gateway (/api/*, /query, /inventory,
  // /sospechosos) NO se hace aca con rewrites(). Las rewrites de Next.js
  // se resuelven en BUILD TIME (quedan hardcodeadas en
  // .next/routes-manifest.json dentro de la imagen), asi que un env var
  // distinto en runtime (docker-compose, .env, etc.) nunca se aplicaba y
  // el proxy quedaba pegado al valor default (http://localhost:8200)
  // usado durante el build -> ECONNREFUSED en produccion/Cloudflare.
  //
  // El proxy real esta en Route Handlers (app/api/[...path]/route.ts,
  // app/query/route.ts, app/inventory/route.ts, app/sospechosos/route.ts)
  // que si leen process.env.API_GATEWAY_INTERNAL_URL en cada request
  // (runtime), via el helper compartido lib/proxy-server.ts.
};

module.exports = nextConfig;
