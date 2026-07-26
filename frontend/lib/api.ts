// Cliente HTTP del frontend.
//
// BASE es "" por default: las llamadas quedan relativas (/api/...) y las
// resuelve el mismo origen (localhost:3000 en dev, o el dominio publico
// detras de Cloudflare en prod). next.config.js tiene un rewrite que
// reenvia /api/:path* al api-gateway interno, asi que esto funciona sin
// cambios sin importar desde que dominio se acceda.
//
// Si en algun caso puntual se necesita pegarle a otra URL absoluta (poco
// comun), se puede seguir seteando NEXT_PUBLIC_API_GATEWAY_URL.
const BASE = process.env.NEXT_PUBLIC_API_GATEWAY_URL || "";
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || "";

export class ApiError extends Error {
  status: number;
  body: string;
  path: string;

  constructor(status: number, body: string, path: string) {
    super(`API error ${status} en ${path}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.path = path;
  }
}

export async function api<T = any>(
  path: string,
  opts: RequestInit = {}
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(opts.headers as Record<string, string> | undefined),
  };
  if (API_KEY) headers["X-API-Key"] = API_KEY;

  const r = await fetch(`${BASE}${path}`, { ...opts, headers });

  if (!r.ok) {
    const body = await r.text().catch(() => "");
    throw new ApiError(r.status, body, path);
  }

  // 204 / respuestas sin body
  if (r.status === 204) return undefined as unknown as T;

  return r.json();
}
