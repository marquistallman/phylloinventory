"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { Logo } from "@/components/Logo";

interface Bodega {
  id: number;
  nombre: string;
}

export default function HomePage() {
  const router = useRouter();
  const [bodegas, setBodegas] = useState<Bodega[]>([]);
  const [selectedBodega, setSelectedBodega] = useState<number | null>(null);
  const [loadingBodegas, setLoadingBodegas] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dropdownOpen, setDropdownOpen] = useState(false);

  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    loadBodegas();
  }, []);

  // Cerrar dropdown al click fuera
  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  async function loadBodegas() {
    setLoadingBodegas(true);
    setError(null);
    try {
      const data = await api<Bodega[]>("/api/bodegas");
      setBodegas(data);
    } catch (e) {
      console.error("Error cargando bodegas:", e);
      setError("No se pudieron cargar las bodegas. Reintentá.");
    } finally {
      setLoadingBodegas(false);
    }
  }

  async function handleEmpezar() {
    if (selectedBodega == null) return;
    setSubmitting(true);
    setError(null);
    try {
      const data = await api<{ sesion_id: number }>("/api/sesion/iniciar", {
        method: "POST",
        body: JSON.stringify({
          bodega_id: selectedBodega,
          iniciada_por: "operador",
        }),
      });
      router.push(`/contar/${data.sesion_id}`);
    } catch (e) {
      console.error("Error iniciando sesión:", e);
      setError("No se pudo iniciar la sesión. Reintentá.");
      setSubmitting(false);
    }
  }

  const selectedNombre = bodegas.find((b) => b.id === selectedBodega)?.nombre;

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="bg-bAzul text-white px-4 py-3 flex items-center justify-between sticky top-0 z-20">
        <div className="flex items-center">
          <Logo size="sm" href={null} />
        </div>
        <a
          href="/admin"
          className="text-white font-semibold hover:underline text-base"
        >
          Admin
        </a>
      </header>

      {/* Main */}
      <main className="flex-1 flex flex-col px-6 py-16 max-w-md mx-auto w-full">
        <h1 className="text-4xl font-bold text-gray-900 leading-tight text-center">
          ¡Bienvenido!
        </h1>
        <p className="text-gray-800 text-lg mt-2 mb-12 text-center">
          Inicia un nuevo conteo
        </p>

        {/* Custom Dropdown de bodega */}
        <div className="mb-6 relative" ref={dropdownRef}>
          <label className="block text-sm font-semibold text-gray-900 mb-2">
            Bodega
          </label>

          {/* Trigger */}
          <button
            type="button"
            onClick={() => !loadingBodegas && setDropdownOpen((o) => !o)}
            disabled={loadingBodegas}
            className="w-full bg-bAzul text-white font-semibold py-3 px-4 pr-10 rounded-lg flex items-center justify-between disabled:opacity-60 transition focus:outline-none focus:ring-2 focus:ring-bAmarillo"
            aria-haspopup="listbox"
            aria-expanded={dropdownOpen}
          >
            <span className={selectedBodega == null ? "opacity-90" : ""}>
              {loadingBodegas
                ? "Cargando..."
                : selectedNombre || "Seleccioná una bodega"}
            </span>
            <svg
              className={`w-5 h-5 text-white transition-transform ${
                dropdownOpen ? "rotate-180" : ""
              }`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2.5}
                d="M19 9l-7 7-7-7"
              />
            </svg>
          </button>

          {/* Lista desplegable */}
          {dropdownOpen && (
            <ul
              className="absolute z-30 left-0 right-0 mt-1 bg-white border-2 border-bAzul rounded-lg shadow-xl max-h-72 overflow-y-auto"
              role="listbox"
            >
              {bodegas.length === 0 ? (
                <li className="px-4 py-3 text-gray-500 text-center text-sm">
                  No hay bodegas disponibles
                </li>
              ) : (
                bodegas.map((b) => {
                  const isSelected = b.id === selectedBodega;
                  return (
                    <li key={b.id}>
                      <button
                        type="button"
                        onClick={() => {
                          setSelectedBodega(b.id);
                          setDropdownOpen(false);
                        }}
                        className={`w-full text-left px-4 py-3 transition flex items-center justify-between ${
                          isSelected
                            ? "bg-bAzul text-white font-semibold"
                            : "text-gray-900 hover:bg-blue-50"
                        }`}
                        role="option"
                        aria-selected={isSelected}
                      >
                        <span>{b.nombre}</span>
                        {isSelected && <span className="text-bAmarillo">✓</span>}
                      </button>
                    </li>
                  );
                })
              )}
            </ul>
          )}
        </div>

        {/* CTA */}
        <button
          onClick={handleEmpezar}
          disabled={selectedBodega == null || submitting}
          className="w-full bg-bAmarillo hover:brightness-95 disabled:bg-gray-300 disabled:cursor-not-allowed text-gray-900 font-bold py-4 rounded-lg text-lg transition mb-10"
        >
          {submitting ? "Iniciando..." : "▶ Empezar conteo"}
        </button>

        {/* Error */}
        {error && (
          <p className="text-red-600 text-sm text-center mb-4">{error}</p>
        )}

        {/* Divider */}
        <div className="flex items-center gap-3 my-4">
          <div className="flex-1 h-px bg-gray-300" />
          <span className="text-gray-500 text-sm">o</span>
          <div className="flex-1 h-px bg-gray-300" />
        </div>

        <a
          href="/sesiones"
          className="text-center text-bAzul font-semibold hover:underline mt-2"
        >
          Ver sesiones anteriores
        </a>
      </main>
    </div>
  );
}
