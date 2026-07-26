// Helper de proxy server-side, usado por los Route Handlers en app/api/,
// app/query/, app/inventory/ y app/sospechosos/.
//
// Por que existe: las rewrites de next.config.js se resuelven en BUILD TIME
// (quedan hardcodeadas en .next/routes-manifest.json dentro de la imagen),
// asi que un env var distinto en runtime (docker compose, .env, etc.) nunca
// se llega a aplicar y el proxy sigue apuntando al valor default
// (http://localhost:8200) usado durante el build -> ECONNREFUSED.
//
// Un Route Handler si lee process.env en cada request, en runtime real, que
// es lo que se necesita para poder cambiar de backend sin rebuildear.
import { NextRequest } from "next/server";

export function gatewayTarget(): string {
  return process.env.API_GATEWAY_INTERNAL_URL || "http://localhost:8200";
}

export async function proxyToGateway(req: NextRequest, gatewayPath: string): Promise<Response> {
  const url = `${gatewayTarget()}${gatewayPath}${req.nextUrl.search}`;

  const headers = new Headers(req.headers);
  headers.delete("host");
  headers.delete("connection");
  headers.delete("content-length");

  const hasBody = !["GET", "HEAD"].includes(req.method);

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: req.method,
      headers,
      body: hasBody ? req.body : undefined,
      // Requerido por undici/Node fetch para mandar un ReadableStream como body.
      duplex: hasBody ? "half" : undefined,
      redirect: "manual",
      cache: "no-store",
    } as RequestInit);
  } catch (e) {
    console.error(`Failed to proxy ${url}`, e);
    return Response.json(
      { detail: "No se pudo conectar con el api-gateway" },
      { status: 502 }
    );
  }

  const respHeaders = new Headers(upstream.headers);
  respHeaders.delete("content-encoding");
  respHeaders.delete("transfer-encoding");
  respHeaders.delete("connection");

  return new Response(upstream.body, {
    status: upstream.status,
    headers: respHeaders,
  });
}
