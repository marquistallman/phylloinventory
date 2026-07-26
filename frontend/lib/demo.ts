"use client";

import { useEffect, useState } from "react";

// Modo demo para grabar el pitch sin depender de que STT/LLM/Kalman
// respondan bien en vivo. Se activa agregando ?demo=1 a la URL (ej.
// http://localhost:3000/?demo=1) y se propaga automaticamente entre
// paginas via withDemo() en los links/redirects de la app.
//
// Leemos window.location.search en vez de next/navigation's
// useSearchParams() a proposito: ese hook exige envolver la pagina en un
// <Suspense>, y no vale la pena la complejidad para algo que es solo un
// interruptor de demo.
export function useDemoMode(): boolean {
  const [demo, setDemo] = useState(false);
  useEffect(() => {
    setDemo(new URLSearchParams(window.location.search).get("demo") === "1");
  }, []);
  return demo;
}

export function withDemo(path: string, demo: boolean): string {
  if (!demo) return path;
  return path.includes("?") ? `${path}&demo=1` : `${path}?demo=1`;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

// ─── Estado compartido del conteo mockeado (Voz → Manual) ────────────────
//
// El mock de Voz y el de Manual viven en paginas distintas (navegacion real
// entre rutas), asi que para que lo que se "dijo" por voz se refleje despues
// en el CRM, lo persistimos en sessionStorage (sobrevive la navegacion,
// se limpia solo al cerrar la pestaña).

export interface DemoRegistro {
  nombre: string;
  cantidad: number;
  unidad: string;
  estado: "ok" | "alert";
  stockSistema: number;
  timestamp: number;
}

const DEMO_REGISTRO_KEY = "blink_demo_registro";

export function setDemoRegistro(r: DemoRegistro): void {
  try {
    sessionStorage.setItem(DEMO_REGISTRO_KEY, JSON.stringify(r));
  } catch {
    // sessionStorage puede no estar disponible (SSR, modo privado, etc.)
  }
}

export function getDemoRegistro(): DemoRegistro | null {
  try {
    const raw = sessionStorage.getItem(DEMO_REGISTRO_KEY);
    return raw ? (JSON.parse(raw) as DemoRegistro) : null;
  } catch {
    return null;
  }
}

//  Hook chico para leer el registro guardado en cuanto el modo demo se
//  confirma (evita duplicar este patron de useEffect en cada pagina que
//  necesita reflejar el resultado del mock de Voz).
export function useDemoOverlay(demo: boolean): DemoRegistro | null {
  const [overlay, setOverlay] = useState<DemoRegistro | null>(null);
  useEffect(() => {
    if (!demo) return;
    setOverlay(getDemoRegistro());
  }, [demo]);
  return overlay;
}

interface ProductoOverlayable {
  nombre: string;
  stock_contado?: number | null;
  estado_conteo?: string;
}

//  Superpone el resultado del mock de Voz sobre un producto real de la DB,
//  para que el CRM/Buscar muestren "lo que deberia ser" despues del comando
//  de voz en vez del stock crudo sin tocar. Match exacto (no substring): no
//  se puede pintar "MANZANA" y de paso "MANZANA ROYAL".
export function aplicarOverlayDemo<T extends ProductoOverlayable>(
  p: T,
  overlay: DemoRegistro | null
): T {
  if (!overlay) return p;
  if (p.nombre.trim().toLowerCase() !== overlay.nombre.trim().toLowerCase()) return p;
  return {
    ...p,
    stock_contado: overlay.cantidad,
    estado_conteo: overlay.estado === "alert" ? "alerta" : "contado",
  };
}
