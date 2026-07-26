// Proxy de /query (raiz, no /api/*) hacia el api-gateway. El endpoint es
// el "cerebro" generico (texto libre -> tool_calls / pending / raw_output),
// el mismo que usa la CLI. Ver frontend/lib/proxy-server.ts.
import { NextRequest } from "next/server";
import { proxyToGateway } from "@/lib/proxy-server";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  return proxyToGateway(req, "/query");
}
