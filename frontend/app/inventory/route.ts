// Proxy de /inventory (raiz, no /api/*) hacia el api-gateway. Usado por
// el modo Voz para responder consultas de stock. Ver lib/proxy-server.ts.
import { NextRequest } from "next/server";
import { proxyToGateway } from "@/lib/proxy-server";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  return proxyToGateway(req, "/inventory");
}
