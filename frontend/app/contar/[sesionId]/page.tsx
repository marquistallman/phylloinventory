"use client";

import { useState, useEffect, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";
import { Logo } from "@/components/Logo";
import { MicIcon, KeyboardIcon, SearchIcon, StopIcon } from "@/components/Icons";

// ─── Tipos ────────────────────────────────────────────────────────────────

interface Producto {
  id: number;
  nombre: string;
  codigo_articulo?: string;
  unidad: string;
  stock_sistema: number;
  stock_contado?: number | null;
  estado_conteo: "pendiente" | "contado" | "alerta";
}

interface SesionEstado {
  sesion_id: number;
  bodega_id: number;
  estado: string;
  iniciada_por?: string;
  total_productos: number;
  contados: number;
  aceptados: number;
  alertas: number;
  pendientes: number;
}

interface RegistroLocal {
  id: number;
  nombre: string;
  cantidad: number;
  unidad: string;
  estado: "loading" | "ok" | "alert" | "error";
  timestamp: number;
}

type FormState = "idle" | "loading" | "success" | "error";

// ─── Página ───────────────────────────────────────────────────────────────

export default function ContarPage() {
  const params = useParams();
  const router = useRouter();
  const sesionId = Number(params.sesionId);

  const [sesion, setSesion] = useState<SesionEstado | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [productos, setProductos] = useState<Producto[]>([]);
  const [searching, setSearching] = useState(false);

  const [productoExpandido, setProductoExpandido] = useState<Producto | null>(null);
  const [cantidad, setCantidad] = useState("");
  const [formState, setFormState] = useState<FormState>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const [registros, setRegistros] = useState<RegistroLocal[]>([]);
  const contadorIdRef = useRef(0);

  // ── Cargar estado de la sesión (polling cada 2s) ──────────────────────
  useEffect(() => {
    loadSesion();
    const interval = setInterval(loadSesion, 2000);
    return () => clearInterval(interval);
  }, [sesionId]);

  async function loadSesion() {
    try {
      const data = await api<SesionEstado>(`/api/sesion/${sesionId}/estado`);
      setSesion(data);
    } catch (e) {
      console.error("Error cargando sesión:", e);
    }
  }

  // ── Catálogo con debounce 300ms ───────────────────────────────────────
  useEffect(() => {
    if (!sesion) return;
    const timer = setTimeout(() => loadProductos(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery, sesion?.bodega_id]);

  async function loadProductos(q: string) {
    if (!sesion) return;
    setSearching(true);
    try {
      const qs = new URLSearchParams({ sesion_id: String(sesionId) });
      if (q.trim()) qs.set("q", q.trim());
      const data = await api<Producto[]>(
        `/api/catalogo/bodega/${sesion.bodega_id}?${qs.toString()}`
      );
      setProductos(data);
    } catch (e) {
      console.error("Error cargando productos:", e);
    } finally {
      setSearching(false);
    }
  }

  // ── Acciones del form ─────────────────────────────────────────────────
  function abrirForm(producto: Producto) {
    setProductoExpandido(producto);
    setCantidad("");
    setFormState("idle");
    setErrorMsg(null);
  }

  function cerrarForm() {
    if (formState === "loading") return; // no cerrar mientras carga
    setProductoExpandido(null);
    setCantidad("");
    setFormState("idle");
    setErrorMsg(null);
  }

  async function handleRegistrar() {
    if (!productoExpandido || !cantidad) return;

    const id = ++contadorIdRef.current;
    const nuevoRegistro: RegistroLocal = {
      id,
      nombre: productoExpandido.nombre,
      cantidad: parseFloat(cantidad),
      unidad: productoExpandido.unidad,
      estado: "loading",
      timestamp: Date.now(),
    };
    setRegistros((prev) => [nuevoRegistro, ...prev].slice(0, 5));
    setFormState("loading");

    try {
      const r = await api<{ pending_id: number; success: boolean }>(
        "/api/sesion/registrar-manual",
        {
          method: "POST",
          body: JSON.stringify({
            sesion_id: sesionId,
            producto_id: productoExpandido.id,
            cantidad: parseFloat(cantidad),
            unidad: productoExpandido.unidad,
          }),
        }
      );

      const result = await pollPending(r.pending_id);

      if (result.status === "ACEPTADA" || result.status === "CONFIRMADA_MANUAL") {
        setRegistros((prev) =>
          prev.map((x) => (x.id === id ? { ...x, estado: "ok" } : x))
        );
        setFormState("success");
        loadProductos(searchQuery);
        loadSesion();
      } else if (result.status === "SOSPECHOSA") {
        // Por ahora lo marcamos como alerta; el modal de confirmación
        // se arma en otra iteración.
        setRegistros((prev) =>
          prev.map((x) => (x.id === id ? { ...x, estado: "alert" } : x))
        );
        setFormState("success");
        loadProductos(searchQuery);
        loadSesion();
      } else if (result.status === "RECHAZADA") {
        setRegistros((prev) =>
          prev.map((x) => (x.id === id ? { ...x, estado: "error" } : x))
        );
        setFormState("error");
        setErrorMsg("Rechazado");
      } else {
        setRegistros((prev) =>
          prev.map((x) => (x.id === id ? { ...x, estado: "error" } : x))
        );
        setFormState("error");
        setErrorMsg("Estado inesperado");
      }
    } catch (e: any) {
      console.error("Error registrando:", e);
      setRegistros((prev) =>
        prev.map((x) => (x.id === id ? { ...x, estado: "error" } : x))
      );
      setFormState("error");
      setErrorMsg(e?.message || "Error de red");
    }
  }

  async function pollPending(id: number): Promise<{ status: string }> {
    const start = Date.now();
    while (true) {
      const data = await api<{ status: string }>(`/api/pending/${id}`);
      if (data.status !== "PENDING") return data;
      if (Date.now() - start > 15000) throw new Error("Timeout esperando Kalman");
      await new Promise((r) => setTimeout(r, 200));
    }
  }

  async function handleFinalizar() {
    if (!confirm("¿Finalizar la sesión de conteo?")) return;
    try {
      await api("/api/sesion/finalizar", {
        method: "POST",
        body: JSON.stringify({ sesion_id: sesionId }),
      });
      router.push(`/contar/${sesionId}/reporte`);
    } catch (e) {
      console.error("Error finalizando:", e);
      alert("No se pudo finalizar. Reintentá.");
    }
  }

  // ── Render ────────────────────────────────────────────────────────────
  const aceptados = sesion?.aceptados ?? 0;
  const alertas = sesion?.alertas ?? 0;
  const contados = sesion?.contados ?? 0;
  const total = sesion?.total_productos ?? 0;
  const porcentaje = total > 0 ? Math.round((contados / total) * 100) : 0;

  return (
    <div className="min-h-screen flex flex-col bg-white">
      {/* Header */}
      <header className="bg-bAzul text-white px-4 py-3 flex items-center justify-between sticky top-0 z-20">
        <Link href="/" className="flex items-center">
          <Logo size="sm" href={null} />
        </Link>
        <Link href="/admin" className="text-white font-semibold hover:underline">
          Admin
        </Link>
      </header>

      {/* Sub-header con info de sesión */}
      <div className="max-w-2xl mx-auto w-full px-4 py-3 border-b border-gray-200 flex items-center gap-2">
        <Link
          href="/"
          className="w-10 h-10 rounded-full bg-gray-100 flex items-center justify-center text-gray-700 text-xl shrink-0"
          aria-label="Volver"
        >
          ←
        </Link>
        <h1 className="flex-1 text-center font-bold text-base text-gray-900">
          Sesión #{sesionId} — {sesion ? `Bodega ${sesion.bodega_id}` : "..."}
        </h1>
        <button
          onClick={handleFinalizar}
          className="w-10 h-10 bg-bAmarillo rounded-full flex items-center justify-center hover:brightness-95 shrink-0"
          aria-label="Finalizar conteo"
          title="Finalizar conteo"
        >
          <StopIcon size={18} color="#1F2937" strokeColor="#1F2937" />
        </button>
      </div>

      {/* Progreso */}
      <div className="max-w-2xl mx-auto w-full px-4 py-3 border-b border-gray-200">
        <div className="flex items-center justify-between text-sm mb-1">
          <span className="text-gray-700 font-medium">Progreso</span>
          <span className="text-gray-900 font-bold tabular-nums">
            {contados}/{total}
          </span>
        </div>
        <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
          <div
            className="h-full bg-bAmarillo transition-all"
            style={{ width: `${porcentaje}%` }}
          />
        </div>
        <div className="flex items-center gap-4 mt-2 text-sm">
          <span className="text-green-600 font-medium">
            ✓ {aceptados} aceptadas
          </span>
          <span className="text-yellow-600 font-medium">
            ⚠ {alertas} alertas
          </span>
        </div>
      </div>

      {/* Tabs */}
      <div className="max-w-2xl mx-auto w-full flex border-b border-gray-200 bg-white sticky top-[52px] z-10">
        <Link
          href={`/contar/${sesionId}/voz`}
          className="flex-1 py-3 text-center font-semibold border-b-2 text-gray-600 border-transparent hover:text-gray-900 flex items-center justify-center gap-1.5"
        >
          <MicIcon size={22} />Voz
        </Link>
        <button
          className="flex-1 py-3 text-center font-semibold border-b-2 text-bAzul border-bAzul flex items-center justify-center gap-1.5"
        >
          <KeyboardIcon size={22} />Manual
        </button>
        <Link
          href={`/contar/${sesionId}/buscar`}
          className="flex-1 py-3 text-center font-semibold border-b-2 text-gray-600 border-transparent hover:text-gray-900 flex items-center justify-center gap-1.5"
        >
          <SearchIcon size={22} />Buscar
        </Link>
      </div>

      {/* Contenido */}
      <main className="flex-1 px-4 py-4 max-w-2xl mx-auto w-full">
        {/* Buscador */}
        <div className="relative mb-6">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none">
            <SearchIcon size={20} color="#9CA3AF" strokeColor="#9CA3AF" />
          </span>
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Buscar producto..."
            className="w-full pl-11 pr-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-base focus:outline-none focus:ring-2 focus:ring-bAzul"
          />
        </div>

        {/* Últimos registros */}
        {registros.length > 0 && (
          <div className="mb-6">
            <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
              Últimos registros
            </p>
            <div className="space-y-1">
              {registros.map((r) => (
                <div
                  key={r.id}
                  className="flex items-center justify-between text-sm"
                >
                  <span className="text-gray-800">
                    {r.nombre} × {r.cantidad} {r.unidad}
                  </span>
                  <span className="text-lg">
                    {r.estado === "loading" && (
                      <span className="inline-block w-5 h-5 border-2 border-gray-300 border-t-bAzul rounded-full animate-spin" />
                    )}
                    {r.estado === "ok" && <span className="text-green-600">✓</span>}
                    {r.estado === "alert" && (
                      <span className="text-yellow-600">⚠</span>
                    )}
                    {r.estado === "error" && <span className="text-red-600">✕</span>}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Resultados */}
        <div>
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
            Resultados{productos.length > 0 && ` (${productos.length})`}
          </p>
          {searching && productos.length === 0 ? (
            <p className="text-gray-500 text-sm py-4">Buscando...</p>
          ) : productos.length === 0 ? (
            <p className="text-gray-500 text-sm py-4">
              {searchQuery
                ? `Sin resultados para "${searchQuery}"`
                : "No hay productos para mostrar"}
            </p>
          ) : (
            <div className="space-y-2">
              {productos.map((p) => (
                <ProductoCard
                  key={p.id}
                  producto={p}
                  expandido={productoExpandido?.id === p.id}
                  onToggle={() =>
                    productoExpandido?.id === p.id
                      ? cerrarForm()
                      : abrirForm(p)
                  }
                >
                  {productoExpandido?.id === p.id && (
                    <RegisterForm
                      cantidad={cantidad}
                      setCantidad={setCantidad}
                      formState={formState}
                      errorMsg={errorMsg}
                      onRegistrar={handleRegistrar}
                      onCancelar={cerrarForm}
                      disabled={formState === "loading"}
                    />
                  )}
                </ProductoCard>
              ))}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}

// ─── Componentes auxiliares ───────────────────────────────────────────────

function TabButton({
  icon,
  label,
  active,
  disabled,
}: {
  icon: React.ReactNode;
  label: string;
  active?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      disabled={disabled}
      className={`flex-1 py-3 text-center font-semibold border-b-2 transition flex items-center justify-center gap-1.5 ${
        active
          ? "text-bAzul border-bAzul"
          : disabled
          ? "text-gray-400 border-transparent cursor-not-allowed"
          : "text-gray-600 border-transparent hover:text-gray-900"
      }`}
    >
      {icon}
      {label}
    </button>
  );
}

function ProductoCard({
  producto,
  expandido,
  onToggle,
  children,
}: {
  producto: Producto;
  expandido: boolean;
  onToggle: () => void;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={`rounded-lg border ${
        expandido ? "border-bAzul" : "border-gray-200"
      } overflow-hidden`}
    >
      <button
        onClick={onToggle}
        className="w-full px-4 py-3 flex items-center justify-between text-left hover:bg-gray-50"
      >
        <div>
          <p className="font-semibold text-gray-900 uppercase">
            {producto.nombre}
          </p>
          <p className="text-sm text-gray-600">
            Stock sistema: {producto.stock_sistema} {producto.unidad}
          </p>
        </div>
        <div className="w-9 h-9 bg-bAmarillo rounded flex items-center justify-center text-gray-900 text-xl font-bold">
          {expandido ? "×" : "+"}
        </div>
      </button>
      {expandido && children}
    </div>
  );
}

function RegisterForm({
  cantidad,
  setCantidad,
  formState,
  errorMsg,
  onRegistrar,
  onCancelar,
  disabled,
}: {
  cantidad: string;
  setCantidad: (v: string) => void;
  formState: FormState;
  errorMsg: string | null;
  onRegistrar: () => void;
  onCancelar: () => void;
  disabled: boolean;
}) {
  return (
    <div className="border-t border-gray-200 bg-gray-50 p-4">
      <div className="grid grid-cols-[auto_1fr_auto] gap-4 items-center mb-4">
        <span className="text-gray-800 font-medium">Cantidad</span>
        <input
          type="number"
          inputMode="decimal"
          value={cantidad}
          onChange={(e) => setCantidad(e.target.value)}
          disabled={disabled}
          placeholder="5"
          step="any"
          className="w-20 px-3 py-2 border border-gray-300 rounded text-center text-base focus:outline-none focus:ring-2 focus:ring-bAzul disabled:bg-gray-100"
          autoFocus
        />
        {/* Estado al lado de cantidad */}
        <div className="min-w-[120px] flex justify-end">
          {formState === "loading" && (
            <span
              className="inline-block w-7 h-7 border-3 border-gray-300 border-t-bAmarillo rounded-full animate-spin"
              aria-label="Cargando"
            />
          )}
          {formState === "success" && (
            <span className="text-green-600 font-semibold text-sm flex items-center gap-1">
              ✓ <span className="text-green-700">Registro exitoso</span>
            </span>
          )}
          {formState === "error" && (
            <span className="text-red-600 font-semibold text-sm flex items-center gap-1">
              ⚠ <span className="text-red-700">{errorMsg || "Error"}</span>
            </span>
          )}
        </div>
      </div>

      <div className="grid grid-cols-[auto_1fr] gap-4 items-center mb-4">
        <span className="text-gray-800 font-medium">Unidad</span>
        <select
          disabled={disabled}
          defaultValue="kg"
          className="w-20 px-3 py-2 border border-gray-300 rounded bg-white text-center text-base focus:outline-none focus:ring-2 focus:ring-bAzul disabled:bg-gray-100"
        >
          <option value="kg">kg</option>
          <option value="L">L</option>
          <option value="u">u</option>
        </select>
      </div>

      <div className="flex items-center justify-between pt-2 border-t border-gray-200">
        <button
          onClick={onCancelar}
          disabled={disabled}
          className="text-red-600 font-semibold py-2 px-4 disabled:opacity-50"
        >
          Cancelar
        </button>
        <button
          onClick={onRegistrar}
          disabled={disabled || !cantidad}
          className="text-green-600 font-semibold py-2 px-4 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Registrar
        </button>
      </div>
    </div>
  );
}
