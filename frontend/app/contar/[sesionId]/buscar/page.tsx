"use client";

import { useState, useEffect } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";
import { Logo } from "@/components/Logo";
import { MicIcon, KeyboardIcon, SearchIcon } from "@/components/Icons";

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

interface Producto {
  id: number;
  nombre: string;
  codigo_articulo?: string;
  unidad: string;
  stock_sistema: number;
  stock_contado?: number | null;
  estado_conteo: "pendiente" | "contado" | "alerta";
}

type Filtro = "todos" | "pendientes" | "contados" | "alertas";

// ─── Página ──────────────────────────────────────────────────────────────

export default function BuscarPage() {
  const params = useParams();
  const sesionId = Number(params.sesionId);

  const [sesion, setSesion] = useState<SesionEstado | null>(null);
  const [search, setSearch] = useState("");
  const [filtro, setFiltro] = useState<Filtro>("todos");
  const [productos, setProductos] = useState<Producto[]>([]);
  const [loading, setLoading] = useState(false);
  const [seleccionado, setSeleccionado] = useState<Producto | null>(null);

  // Cargar sesión (necesitamos bodega_id)
  useEffect(() => {
    api<SesionEstado>(`/api/sesion/${sesionId}/estado`)
      .then(setSesion)
      .catch((e) => console.error("Error cargando sesión:", e));
  }, [sesionId]);

  // Cargar catálogo con debounce. El filtro por estado (pendientes/
  // contados/alertas) NO se manda al backend: se aplica client-side sobre
  // el catalogo completo (ver `productosFiltrados`), porque el backend
  // sólo entiende `solo_pendientes` y antes los chips "Contados"/"Alertas"
  // no filtraban nada (re-pedían la misma lista completa).
  useEffect(() => {
    if (!sesion) return;
    const t = setTimeout(() => loadProductos(), 300);
    return () => clearTimeout(t);
  }, [search, sesion?.bodega_id]);

  async function loadProductos() {
    if (!sesion) return;
    setLoading(true);
    try {
      const qs = new URLSearchParams({ sesion_id: String(sesionId) });
      if (search.trim()) qs.set("q", search.trim());
      const data = await api<Producto[]>(
        `/api/catalogo/bodega/${sesion.bodega_id}?${qs.toString()}`
      );
      setProductos(data);
    } catch (e) {
      console.error("Error cargando productos:", e);
    } finally {
      setLoading(false);
    }
  }

  const FILTRO_A_ESTADO: Record<Exclude<Filtro, "todos">, Producto["estado_conteo"]> = {
    pendientes: "pendiente",
    contados: "contado",
    alertas: "alerta",
  };
  const productosFiltrados =
    filtro === "todos" ? productos : productos.filter((p) => p.estado_conteo === FILTRO_A_ESTADO[filtro]);

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div className="min-h-screen flex flex-col bg-white">
      <Header />
      <SubHeader sesionId={sesionId} />
      <div className="max-w-2xl mx-auto w-full">
        <Tabs sesionId={sesionId} active="buscar" />
      </div>

      <main className="flex-1 px-4 py-4 max-w-2xl mx-auto w-full">
        {/* Buscador grande */}
        <div className="relative mb-4">
          <span className="absolute left-4 top-1/2 -translate-y-1/2 pointer-events-none">
            <SearchIcon size={20} color="#9CA3AF" strokeColor="#9CA3AF" />
          </span>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Buscar producto por nombre o código..."
            autoFocus
            className="w-full pl-12 pr-12 py-4 bg-gray-50 border-2 border-gray-200 rounded-xl text-base focus:outline-none focus:border-bAzul focus:bg-white transition"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              className="absolute right-4 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-xl"
              aria-label="Limpiar búsqueda"
            >
              ×
            </button>
          )}
        </div>

        {/* Filtros chips */}
        <div className="flex gap-2 mb-4 overflow-x-auto pb-1">
          <Chip
            active={filtro === "todos"}
            onClick={() => setFiltro("todos")}
            label="Todos"
            count={sesion?.total_productos}
          />
          <Chip
            active={filtro === "pendientes"}
            onClick={() => setFiltro("pendientes")}
            label="Pendientes"
            count={sesion?.pendientes}
            color="gray"
          />
          <Chip
            active={filtro === "contados"}
            onClick={() => setFiltro("contados")}
            label="Contados"
            count={sesion?.contados}
            color="green"
          />
          <Chip
            active={filtro === "alertas"}
            onClick={() => setFiltro("alertas")}
            label="Alertas"
            count={sesion?.alertas}
            color="yellow"
          />
        </div>

        {/* Contador de resultados */}
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
          {loading
            ? "Buscando..."
            : `${productosFiltrados.length} resultado${productosFiltrados.length === 1 ? "" : "s"}`}
        </p>

        {/* Lista de productos */}
        {productosFiltrados.length === 0 && !loading ? (
          <div className="text-center py-12 text-gray-500">
            {search
              ? `Sin resultados para "${search}"`
              : "No hay productos para mostrar"}
          </div>
        ) : (
          <div className="space-y-2">
            {productosFiltrados.map((p) => (
              <ProductoCard
                key={p.id}
                producto={p}
                onClick={() => setSeleccionado(p)}
              />
            ))}
          </div>
        )}
      </main>

      {/* Drawer de detalle */}
      {seleccionado && (
        <ProductoDrawer
          producto={seleccionado}
          sesionId={sesionId}
          onClose={() => setSeleccionado(null)}
        />
      )}
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

function SubHeader({ sesionId }: { sesionId: number }) {
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
      <Link
        href={`/contar/${sesionId}/voz`}
        className="w-10 h-10 bg-bAmarillo rounded-full flex items-center justify-center hover:brightness-95 shrink-0"
        aria-label="Pasar a voz"
        title="Pasar a modo voz"
      >
        <MicIcon size={18} color="#1F2937" strokeColor="#1F2937" />
      </Link>
    </div>
  );
}

function Tabs({ sesionId, active }: { sesionId: number; active: "voz" | "manual" | "buscar" }) {
  const cls = (a: string) =>
    `flex-1 py-3 text-center font-semibold border-b-2 transition flex items-center justify-center gap-1.5 ${
      a === active
        ? "text-bAzul border-bAzul"
        : "text-gray-600 border-transparent hover:text-gray-900"
    }`;
  return (
    <div className="flex border-b border-gray-200 bg-white sticky top-[52px] z-10">
      <Link href={`/contar/${sesionId}/voz`} className={cls("voz")}>
        <MicIcon size={22} />Voz
      </Link>
      <Link href={`/contar/${sesionId}`} className={cls("manual")}>
        <KeyboardIcon size={22} />Manual
      </Link>
      <Link href={`/contar/${sesionId}/buscar`} className={cls("buscar")}>
        <SearchIcon size={22} />Buscar
      </Link>
    </div>
  );
}

function Chip({
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
  color?: "blue" | "green" | "yellow" | "gray";
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
    yellow: {
      active: "bg-bAmarillo text-gray-900 border-bAmarillo",
      inactive: "bg-white text-gray-700 border-gray-300 hover:border-bAmarillo",
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

function ProductoCard({
  producto,
  onClick,
}: {
  producto: Producto;
  onClick: () => void;
}) {
  const estadoBadge = {
    pendiente: { text: "Pendiente", cls: "bg-gray-100 text-gray-700" },
    contado: { text: "Contado", cls: "bg-green-100 text-green-700" },
    alerta: { text: "Alerta", cls: "bg-yellow-100 text-yellow-700" },
  }[producto.estado_conteo];

  return (
    <button
      onClick={onClick}
      className="w-full text-left p-3 border border-gray-200 rounded-lg hover:border-bAzul hover:bg-blue-50/30 transition"
    >
      <div className="flex items-start justify-between gap-2 mb-1">
        <p className="font-semibold text-gray-900 uppercase flex-1">
          {producto.nombre}
        </p>
        <span className={`text-xs px-2 py-0.5 rounded-full font-semibold whitespace-nowrap ${estadoBadge.cls}`}>
          {estadoBadge.text}
        </span>
      </div>
      <div className="flex items-center gap-4 text-sm text-gray-600">
        <span>
          <span className="text-gray-500">Stock:</span>{" "}
          <span className="font-semibold text-gray-900 tabular-nums">
            {producto.stock_sistema} {producto.unidad}
          </span>
        </span>
        {producto.stock_contado != null && (
          <span>
            <span className="text-gray-500">Contado:</span>{" "}
            <span className="font-semibold text-gray-900 tabular-nums">
              {producto.stock_contado} {producto.unidad}
            </span>
          </span>
        )}
        {producto.codigo_articulo && (
          <span className="text-gray-400 text-xs ml-auto">
            #{producto.codigo_articulo}
          </span>
        )}
      </div>
    </button>
  );
}

function ProductoDrawer({
  producto,
  sesionId,
  onClose,
}: {
  producto: Producto;
  sesionId: number;
  onClose: () => void;
}) {
  return (
    <>
      <div
        onClick={onClose}
        className="fixed inset-0 bg-black/40 z-30 fade-in"
        aria-hidden="true"
      />
      <div className="fixed bottom-0 left-0 right-0 bg-white z-40 rounded-t-2xl p-5 max-h-[80vh] overflow-y-auto shadow-2xl">
        <div className="w-12 h-1.5 bg-gray-300 rounded-full mx-auto mb-4" />

        <div className="flex items-start justify-between mb-4">
          <div>
            <h2 className="text-xl font-bold text-gray-900 uppercase">
              {producto.nombre}
            </h2>
            {producto.codigo_articulo && (
              <p className="text-sm text-gray-500">
                Código: {producto.codigo_articulo}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-2xl leading-none"
            aria-label="Cerrar"
          >
            ×
          </button>
        </div>

        <div className="grid grid-cols-2 gap-3 mb-4">
          <div className="bg-gray-50 rounded-lg p-3">
            <p className="text-xs text-gray-500 uppercase">Unidad</p>
            <p className="text-lg font-semibold text-gray-900">
              {producto.unidad}
            </p>
          </div>
          <div className="bg-gray-50 rounded-lg p-3">
            <p className="text-xs text-gray-500 uppercase">Stock sistema</p>
            <p className="text-lg font-semibold text-gray-900 tabular-nums">
              {producto.stock_sistema}
            </p>
          </div>
          {producto.stock_contado != null && (
            <div className="bg-blue-50 rounded-lg p-3 col-span-2">
              <p className="text-xs text-blue-700 uppercase">Stock contado</p>
              <p className="text-2xl font-bold text-blue-900 tabular-nums">
                {producto.stock_contado} {producto.unidad}
              </p>
              {producto.stock_contado !== producto.stock_sistema && (
                <p className="text-xs text-blue-700 mt-1">
                  Diferencia:{" "}
                  <span
                    className={
                      producto.stock_contado > producto.stock_sistema
                        ? "text-green-700 font-semibold"
                        : "text-red-700 font-semibold"
                    }
                  >
                    {producto.stock_contado - producto.stock_sistema > 0 ? "+" : ""}
                    {(producto.stock_contado - producto.stock_sistema).toFixed(2)}
                  </span>
                </p>
              )}
            </div>
          )}
        </div>

        <Link
          href={`/contar/${sesionId}`}
          className="block w-full py-3 bg-bAmarillo text-gray-900 font-bold text-center rounded-lg hover:brightness-95"
        >
          ⌨️ Ir a contar este producto
        </Link>
      </div>
    </>
  );
}
