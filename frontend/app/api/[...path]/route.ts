// Proxy universal para /api/*. Ver frontend/lib/proxy-server.ts para el
// porque de este Route Handler en vez de rewrites() en next.config.js.
import { NextRequest } from "next/server";
import { proxyToGateway } from "@/lib/proxy-server";

export const dynamic = "force-dynamic";

type Ctx = { params: { path: string[] } };

function forward(req: NextRequest, { params }: Ctx) {
  return proxyToGateway(req, `/api/${params.path.join("/")}`);
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
