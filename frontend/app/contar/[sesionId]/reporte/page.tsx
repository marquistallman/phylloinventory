"use client";

import { useState, useEffect } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";
import { Logo } from "@/components/Logo";
import { DownloadIcon, StopIcon } from "@/components/Icons";

// ─── Tipos ───────────────────────────────────────────────────────────────

interface SesionEstado {
  sesion_id: number;
  bodega_id: number;
  estado: string;
  total_productos: number;
  contados: number;
  aceptados: number;
  alertas: number;
  pendientes: number;
}

interface Sospechoso {
  pending_id: number;
  nombre: string;
  unidad: string;
  cantidad_contada: number;
  stock_sistema: number;
  diferencia: number;
  residual: number;
  umbral: number;
  decision: string;
  created_at?: string;
}

interface Contado {
  nombre: string;
  unidad: string;
  stock_sistema: number;
  stock_contado: number;
  diferencia: number;
  decision_kalman: string;
}

interface NoContado {
  nombre: string;
  unidad: string;
  stock_sistema: number;
  decision_kalman: string;
}

type AccionAlerta = "confirmar" | "rechazar" | null;

// ─── Página ──────────────────────────────────────────────────────────────

export default function ReportePage() {
  const params = useParams();
  const sesionId = Number(params.sesionId);

  const [estado, setEstado] = useState<SesionEstado | null>(null);
  const [sospechosos, setSospechosos] = useState<Sospechoso[]>([]);
  const [contados, setContados] = useState<Contado[]>([]);
  const [noContados, setNoContados] = useState<NoContado[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [procesando, setProcesando] = useState<{ idx: number; accion: AccionAlerta }>({
    idx: -1,
    accion: null,
  });

  useEffect(() => {
    loadAll();
  }, [sesionId]);

  async function loadAll() {
    setLoading(true);
    setError(null);
    try {
      const [e, s, d] = await Promise.all([
        api<SesionEstado>(`/api/sesion/${sesionId}/estado`),
        api<{ sospechosos: Sospechoso[] }>(`/api/reporte/sospechosos/${sesionId}`),
        api<{ contados: Contado[]; no_contados: NoContado[] }>(
          `/api/reporte/diferencias/${sesionId}`
        ),
      ]);
      setEstado(e);
      setSospechosos(s.sospechosos);
      setContados(d.contados);
      setNoContados(d.no_contados);
    } catch (err: any) {
      console.error("Error cargando reporte:", err);
      setError(err?.message || "Error cargando el reporte");
    } finally {
      setLoading(false);
    }
  }

  async function handleAccionAlerta(idx: number, accion: AccionAlerta) {
    if (!accion) return;
    const s = sospechosos[idx];
    if (!s) return;
    setProcesando({ idx, accion });
    try {
      //  Igual mecanismo que la CLI para resolver una alerta SOSPECHOSA:
      //  no hay un endpoint dedicado /api/pending/{id}/confirmar — se
      //  reusa /query con texto "si"/"no" + pending_alert (needle/openrouter
      //  ya saben resolver esto via confirmar_movimiento).
      const confirmar = accion === "confirmar";
      const puntaje = s.umbral ? Math.abs(s.residual) / s.umbral : 0;
      const resp = await api<{ pending: Array<{ pending_id: number; tool_name: string }> }>(
        "/query",
        {
          method: "POST",
          body: JSON.stringify({
            text: confirmar ? "si" : "no",
            session_id: String(sesionId),
            bodega_id: estado?.bodega_id,
            pending_alert: {
              pending_id: s.pending_id,
              producto: s.nombre,
              cantidad: s.cantidad_contada,
              tipo: null,
              residual: s.residual,
              puntaje_riesgo: puntaje,
            },
          }),
        }
      );

      const confirmPending = resp.pending?.find((p) => p.tool_name === "confirmar_movimiento");
      if (!confirmPending) {
        throw new Error("No se pudo procesar la confirmación.");
      }
      await pollPendingConfirmacion(confirmPending.pending_id);

      setSospechosos((prev) => prev.filter((_, i) => i !== idx));
      loadAll();
    } catch (e: any) {
      console.error("Error procesando alerta:", e);
      setError(e?.message || "No se pudo procesar la alerta. Reintentá.");
    } finally {
      setProcesando({ idx: -1, accion: null });
    }
  }

  async function pollPendingConfirmacion(id: number): Promise<{ status: string }> {
    const start = Date.now();
    while (true) {
      const data = await api<{ status: string }>(`/api/pending/${id}`);
      if (data.status !== "PENDING") return data;
      if (Date.now() - start > 15000) throw new Error("Timeout esperando confirmación");
      await new Promise((r) => setTimeout(r, 200));
    }
  }

  function handleExportar() {
    // TODO: implementar export real (CSV / PDF)
    const csv = exportarCSV();
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `reporte-sesion-${sesionId}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function exportarCSV(): string {
    const lines: string[] = [];
    lines.push(`Sesion,${sesionId}`);
    lines.push(`Generado,${new Date().toISOString()}`);
    lines.push("");
    lines.push("=== CONTADOS ===");
    lines.push("Producto,Unidad,Stock Sistema,Stock Contado,Diferencia,Decision");
    contados.forEach((c) =>
      lines.push(`${c.nombre},${c.unidad},${c.stock_sistema},${c.stock_contado},${c.diferencia},${c.decision_kalman}`)
    );
    lines.push("");
    lines.push("=== NO CONTADOS ===");
    lines.push("Producto,Unidad,Stock Sistema");
    noContados.forEach((p) => lines.push(`${p.nombre},${p.unidad},${p.stock_sistema}`));
    lines.push("");
    lines.push("=== ALERTAS SOSPECHOSAS ===");
    lines.push("Producto,Unidad,Sistema,Contado,Diferencia,Residual,Umbral");
    sospechosos.forEach((s) =>
      lines.push(
        `${s.nombre},${s.unidad},${s.stock_sistema},${s.cantidad_contada},${s.diferencia},${s.residual},${s.umbral}`
      )
    );
    return lines.join("\n");
  }

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div className="min-h-screen flex flex-col bg-white">
      <Header />
      <SubHeader sesionId={sesionId} onExport={handleExportar} />

      <main className="flex-1 px-4 py-6 max-w-2xl mx-auto w-full">
        {error && (
          <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">
            {error}
            <button onClick={loadAll} className="ml-2 underline">
              Reintentar
            </button>
          </div>
        )}

        {/* Resumen */}
        <h2 className="text-lg font-bold text-gray-900 mb-3">Resumen</h2>
        <div className="grid grid-cols-4 gap-2 mb-6">
          <SummaryCard
            color="#0067B1"
            label="total"
            value={estado?.total_productos ?? 0}
            loading={loading}
          />
          <SummaryCard
            color="#10b981"
            label="ok"
            value={estado?.aceptados ?? 0}
            loading={loading}
          />
          <SummaryCard
            color="#FFD000"
            label="alertas"
            value={estado?.alertas ?? 0}
            loading={loading}
          />
          <SummaryCard
            color="#575756"
            label="pendientes"
            value={estado?.pendientes ?? 0}
            loading={loading}
          />
        </div>

        {/* Alertas */}
        <Section
          icon={<span className="text-yellow-600">⚠️</span>}
          title={`Alertas (${sospechosos.length})`}
          defaultOpen
        >
          {sospechosos.length === 0 ? (
            <p className="text-gray-500 text-sm py-2 text-center">
              {loading ? "Cargando..." : "No hay alertas pendientes."}
            </p>
          ) : (
            <div className="space-y-3">
              {sospechosos.map((s, i) => (
                <AlertaCard
                  key={i}
                  sospechoso={s}
                  procesando={procesando.idx === i}
                  accionEnCurso={procesando.accion}
                  onConfirmar={() => handleAccionAlerta(i, "confirmar")}
                  onRechazar={() => handleAccionAlerta(i, "rechazar")}
                />
              ))}
            </div>
          )}
        </Section>

        {/* Contados */}
        <Section
          icon={<span className="text-green-600">✓</span>}
          title={`Contados (${contados.length})`}
          defaultOpen
        >
          {contados.length === 0 ? (
            <p className="text-gray-500 text-sm py-2 text-center">
              {loading ? "Cargando..." : "Sin productos contados."}
            </p>
          ) : (
            <ContadosTable contados={contados} />
          )}
        </Section>

        {/* Pendientes */}
        {noContados.length > 0 && (
          <Section
            icon={<span className="text-gray-500">⏳</span>}
            title={`Pendientes (${noContados.length})`}
            defaultOpen={false}
          >
            <PendientesTable items={noContados} />
          </Section>
        )}

        {/* Footer con volver */}
        <div className="mt-8 flex gap-3">
          <Link
            href={`/contar/${sesionId}`}
            className="flex-1 py-3 text-center border-2 border-gray-300 text-gray-700 font-semibold rounded-lg hover:bg-gray-50"
          >
            ← Volver al conteo
          </Link>
          <Link
            href="/"
            className="flex-1 py-3 text-center bg-bAzul text-white font-semibold rounded-lg hover:brightness-95"
          >
            🏠 Inicio
          </Link>
        </div>
      </main>
    </div>
  );
}

// ─── Componentes ─────────────────────────────────────────────────────────

function Header() {
  return (
    <header className="bg-bAzul text-white px-4 py-3 flex items-center justify-between sticky top-0 z-20">
      <Link href="/" className="flex items-center">
        <Logo size="sm" href={null} />
      </Link>
      <Link href="/admin" className="text-white font-semibold hover:underline">
        Admin
      </Link>
    </header>
  );
}

function SubHeader({ sesionId, onExport }: { sesionId: number; onExport: () => void }) {
  return (
    <div className="max-w-2xl mx-auto w-full px-4 py-3 border-b border-gray-200 flex items-center gap-2">
      <Link
        href={`/contar/${sesionId}`}
        className="w-10 h-10 rounded-full bg-gray-100 flex items-center justify-center text-gray-700 text-xl shrink-0"
        aria-label="Volver"
      >
        ←
      </Link>
      <h1 className="flex-1 text-center font-bold text-base text-gray-900">
        Sesión #{sesionId} — Bodega cocina
      </h1>
      <button
        onClick={onExport}
        className="w-10 h-10 bg-bAmarillo rounded-full flex items-center justify-center hover:brightness-95 shrink-0"
        aria-label="Exportar"
        title="Exportar CSV"
      >
        <DownloadIcon size={18} color="#1F2937" strokeColor="#1F2937" />
      </button>
    </div>
  );
}

function SummaryCard({
  color,
  label,
  value,
  loading,
}: {
  color: string;
  label: string;
  value: number;
  loading?: boolean;
}) {
  const isDark = color === "#575756";
  return (
    <div
      className="rounded-lg p-3 flex flex-col items-center justify-center text-center min-h-[88px]"
      style={{ backgroundColor: color, color: "white" }}
    >
      {loading ? (
        <div className="w-6 h-6 border-2 border-white/40 border-t-white rounded-full animate-spin" />
      ) : (
        <>
          <span className="text-2xl font-bold tabular-nums leading-tight">{value}</span>
          <span className={`text-xs ${isDark ? "text-gray-200" : "text-white/90"} mt-0.5`}>
            {label}
          </span>
        </>
      )}
    </div>
  );
}

function Section({
  icon,
  title,
  defaultOpen,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(!!defaultOpen);
  return (
    <div className="mb-4">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between py-2 text-left"
      >
        <div className="flex items-center gap-2">
          {icon}
          <span className="font-bold text-gray-900">{title}</span>
        </div>
        <span
          className="text-gray-500 text-sm transition-transform"
          style={{ transform: open ? "rotate(0deg)" : "rotate(-90deg)" }}
        >
          ▼
        </span>
      </button>
      {open && <div className="mt-2">{children}</div>}
    </div>
  );
}

function AlertaCard({
  sospechoso,
  procesando,
  accionEnCurso,
  onConfirmar,
  onRechazar,
}: {
  sospechoso: Sospechoso;
  procesando: boolean;
  accionEnCurso: AccionAlerta;
  onConfirmar: () => void;
  onRechazar: () => void;
}) {
  const { nombre, stock_sistema, cantidad_contada, diferencia, unidad } = sospechoso;
  const diffClass = diferencia > 0 ? "text-green-600" : "text-red-600";

  return (
    <div className="border border-gray-200 rounded-lg p-3 bg-white">
      <div className="flex items-center justify-between mb-3">
        <span className="font-semibold text-gray-900 text-lg">{nombre}</span>
        <span className="text-sm text-gray-700">
          sis: {stock_sistema} → cnt: {cantidad_contada}{" "}
          <span className={`font-semibold ${diffClass}`}>
            ({diferencia > 0 ? "+" : ""}
            {diferencia})
          </span>
        </span>
      </div>
      <div className="flex gap-2">
        <button
          onClick={onConfirmar}
          disabled={procesando}
          className="flex-1 py-2 bg-green-600 text-white font-semibold rounded hover:brightness-95 disabled:opacity-50"
        >
          {procesando && accionEnCurso === "confirmar" ? "..." : "Confirmar"}
        </button>
        <button
          onClick={onRechazar}
          disabled={procesando}
          className="flex-1 py-2 bg-red-600 text-white font-semibold rounded hover:brightness-95 disabled:opacity-50"
        >
          {procesando && accionEnCurso === "rechazar" ? "..." : "Rechazar"}
        </button>
      </div>
    </div>
  );
}

function ContadosTable({ contados }: { contados: Contado[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-300">
            <th className="text-left py-2 font-bold text-gray-700">Producto</th>
            <th className="text-right py-2 font-bold text-gray-700">Sis</th>
            <th className="text-right py-2 font-bold text-gray-700">Cnt</th>
            <th className="text-right py-2 font-bold text-gray-700">Dif</th>
          </tr>
        </thead>
        <tbody>
          {contados.map((c, i) => {
            const diffClass =
              c.diferencia > 0
                ? "text-green-600"
                : c.diferencia < 0
                ? "text-red-600"
                : "text-gray-700";
            return (
              <tr key={i} className="border-b border-gray-100">
                <td className="py-2 text-gray-900">{c.nombre}</td>
                <td className="text-right py-2 text-gray-700 tabular-nums">
                  {c.stock_sistema}
                </td>
                <td className="text-right py-2 text-gray-700 tabular-nums">
                  {c.stock_contado}
                </td>
                <td
                  className={`text-right py-2 font-semibold tabular-nums ${diffClass}`}
                >
                  {c.diferencia > 0 ? "+" : ""}
                  {c.diferencia}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PendientesTable({ items }: { items: NoContado[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-300">
            <th className="text-left py-2 font-bold text-gray-700">Producto</th>
            <th className="text-right py-2 font-bold text-gray-700">Stock</th>
          </tr>
        </thead>
        <tbody>
          {items.map((p, i) => (
            <tr key={i} className="border-b border-gray-100">
              <td className="py-2 text-gray-900">{p.nombre}</td>
              <td className="text-right py-2 text-gray-700 tabular-nums">
                {p.stock_sistema} {p.unidad}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
