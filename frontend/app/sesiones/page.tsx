"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { Logo } from "@/components/Logo";

// ─── Tipos ───────────────────────────────────────────────────────────────

interface Sesion {
  id: number;
  bodega_id: number;
  bodega_nombre: string;
  estado: "activa" | "finalizada" | "cancelada";
  iniciada_por?: string;
  creado_en: string;
  finalizado_en?: string | null;
  total_productos?: number;
  contados?: number;
  alertas?: number;
}

// ─── Utilidades ──────────────────────────────────────────────────────────

function formatRelative(iso: string): string {
  const date = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  if (diffSec < 60) return "Hace un momento";
  if (diffMin < 60) return `Hace ${diffMin} min`;
  if (diffHour < 24) return `Hace ${diffHour} h`;
  if (diffDay < 7) return `Hace ${diffDay} d`;
  return date.toLocaleDateString("es-CO", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function formatAbsolute(iso: string): string {
  return new Date(iso).toLocaleString("es-CO", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ─── Página ──────────────────────────────────────────────────────────────

export default function SesionesPage() {
  const [sesiones, setSesiones] = useState<Sesion[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filtro, setFiltro] = useState<"todas" | "activas" | "finalizadas">("todas");

  useEffect(() => {
    loadSesiones();
  }, []);

  async function loadSesiones() {
    setLoading(true);
    setError(null);
    try {
      // Si el backend no tiene este endpoint, podés usar el fallback
      const data = await api<Sesion[]>("/api/sesiones");
      setSesiones(data);
    } catch (e: any) {
      console.error("Error cargando sesiones:", e);
      setError(
        e?.status === 404
          ? "El endpoint /api/sesiones no existe todavía. Hay que agregarlo al backend."
          : "No se pudieron cargar las sesiones."
      );
    } finally {
      setLoading(false);
    }
  }

  const sesionesFiltradas = sesiones.filter((s) => {
    if (filtro === "activas") return s.estado === "activa";
    if (filtro === "finalizadas") return s.estado === "finalizada";
    return true;
  });

  return (
    <div className="min-h-screen flex flex-col bg-gray-50">
      {/* Header */}
      <header className="bg-bAzul text-white px-4 py-3 flex items-center justify-between sticky top-0 z-20">
        <Link href="/" className="flex items-center">
          <Logo size="sm" href={null} />
        </Link>
        <Link href="/admin" className="text-white font-semibold hover:underline">
          Admin
        </Link>
      </header>

      {/* Sub-header */}
      <div className="bg-white max-w-2xl mx-auto w-full px-4 py-3 border-b border-gray-200 flex items-center gap-2">
        <Link
          href="/"
          className="w-10 h-10 rounded-full bg-gray-100 flex items-center justify-center text-gray-700 text-xl shrink-0"
          aria-label="Volver"
        >
          ←
        </Link>
        <h1 className="flex-1 text-center font-bold text-base text-gray-900">
          Sesiones anteriores
        </h1>
        <div className="w-10 h-10 shrink-0" />
      </div>

      {/* Main */}
      <main className="flex-1 max-w-2xl mx-auto w-full px-4 py-6">
        {/* Filtros */}
        <div className="flex gap-2 mb-4">
          <FilterChip
            active={filtro === "todas"}
            onClick={() => setFiltro("todas")}
            label="Todas"
            count={sesiones.length}
          />
          <FilterChip
            active={filtro === "activas"}
            onClick={() => setFiltro("activas")}
            label="Activas"
            color="green"
            count={sesiones.filter((s) => s.estado === "activa").length}
          />
          <FilterChip
            active={filtro === "finalizadas"}
            onClick={() => setFiltro("finalizadas")}
            label="Finalizadas"
            color="gray"
            count={sesiones.filter((s) => s.estado === "finalizada").length}
          />
        </div>

        {/* Loading */}
        {loading && (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div
                key={i}
                className="bg-white rounded-lg border border-gray-200 p-4 animate-pulse"
              >
                <div className="h-4 bg-gray-200 rounded w-1/2 mb-2" />
                <div className="h-3 bg-gray-200 rounded w-1/3" />
              </div>
            ))}
          </div>
        )}

        {/* Error */}
        {!loading && error && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-center">
            <p className="text-red-700 text-sm mb-3">{error}</p>
            <button
              onClick={loadSesiones}
              className="px-4 py-2 bg-red-600 text-white text-sm font-semibold rounded hover:bg-red-700"
            >
              Reintentar
            </button>
          </div>
        )}

        {/* Empty */}
        {!loading && !error && sesionesFiltradas.length === 0 && (
          <div className="text-center py-12">
            <div className="w-16 h-16 mx-auto mb-4 bg-gray-100 rounded-full flex items-center justify-center text-2xl">
              📋
            </div>
            <p className="text-gray-700 font-semibold mb-1">No hay sesiones</p>
            <p className="text-gray-500 text-sm">
              {filtro === "activas"
                ? "No tenés sesiones activas en este momento."
                : filtro === "finalizadas"
                ? "Todavía no finalizaste ninguna sesión."
                : "Empezá tu primera sesión desde la pantalla principal."}
            </p>
            <Link
              href="/"
              className="inline-block mt-4 px-4 py-2 bg-bAmarillo text-gray-900 font-semibold rounded-lg"
            >
              Iniciar nueva sesión
            </Link>
          </div>
        )}

        {/* Lista */}
        {!loading && !error && sesionesFiltradas.length > 0 && (
          <div className="space-y-3">
            {sesionesFiltradas.map((s) => (
              <SesionCard key={s.id} sesion={s} />
            ))}
          </div>
        )}
      </main>
    </div>
  );
}

// ─── Componentes ─────────────────────────────────────────────────────────

function FilterChip({
  active,
  onClick,
  label,
  count,
  color = "blue",
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count?: number;
  color?: "blue" | "green" | "gray";
}) {
  const colorMap: Record<string, { active: string; inactive: string }> = {
    blue: {
      active: "bg-bAzul text-white border-bAzul",
      inactive: "bg-white text-gray-700 border-gray-300 hover:border-bAzul",
    },
    green: {
      active: "bg-green-600 text-white border-green-600",
      inactive: "bg-white text-gray-700 border-gray-300 hover:border-green-600",
    },
    gray: {
      active: "bg-bGris text-white border-bGris",
      inactive: "bg-white text-gray-700 border-gray-300 hover:border-bGris",
    },
  };
  const c = colorMap[color];
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full border-2 text-sm font-semibold whitespace-nowrap transition ${
        active ? c.active : c.inactive
      }`}
    >
      {label}
      {count != null && (
        <span
          className={`text-xs px-1.5 py-0.5 rounded-full ${
            active ? "bg-white/20" : "bg-gray-100"
          }`}
        >
          {count}
        </span>
      )}
    </button>
  );
}

function SesionCard({ sesion }: { sesion: Sesion }) {
  const isActiva = sesion.estado === "activa";
  const total = sesion.total_productos ?? 0;
  const contados = sesion.contados ?? 0;
  const alertas = sesion.alertas ?? 0;
  const porcentaje = total > 0 ? Math.round((contados / total) * 100) : 0;

  return (
    <Link
      href={
        isActiva
          ? `/contar/${sesion.id}`
          : `/contar/${sesion.id}/reporte`
      }
      className="block bg-white rounded-lg border border-gray-200 p-4 hover:border-bAzul hover:shadow-sm transition"
    >
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="flex-1 min-w-0">
          <p className="font-bold text-gray-900 text-base truncate">
            Sesión #{sesion.id} — {sesion.bodega_nombre || `Bodega ${sesion.bodega_id}`}
          </p>
          {sesion.iniciada_por && (
            <p className="text-xs text-gray-500">
              por {sesion.iniciada_por}
            </p>
          )}
        </div>
        <span
          className={`shrink-0 text-xs px-2 py-0.5 rounded-full font-semibold whitespace-nowrap ${
            isActiva
              ? "bg-green-100 text-green-700"
              : "bg-gray-100 text-gray-700"
          }`}
        >
          {isActiva ? "● Activa" : "Finalizada"}
        </span>
      </div>

      <p className="text-xs text-gray-500 mb-3">
        {formatRelative(sesion.creado_en)}
        {sesion.finalizado_en && (
          <> · Finalizada {formatRelative(sesion.finalizado_en)}</>
        )}
      </p>

      {total > 0 && (
        <>
          <div className="h-1.5 bg-gray-200 rounded-full overflow-hidden mb-2">
            <div
              className="h-full bg-bAmarillo transition-all"
              style={{ width: `${porcentaje}%` }}
            />
          </div>
          <div className="flex items-center gap-3 text-xs text-gray-600">
            <span className="tabular-nums">
              <span className="font-semibold text-gray-900">{contados}</span>
              <span className="text-gray-500">/{total}</span> contados
            </span>
            {alertas > 0 && (
              <span className="text-yellow-700 font-semibold">
                ⚠ {alertas} alerta{alertas === 1 ? "" : "s"}
              </span>
            )}
            {isActiva && (
              <span className="ml-auto text-bAzul font-semibold">
                Continuar →
              </span>
            )}
          </div>
        </>
      )}
    </Link>
  );
}
