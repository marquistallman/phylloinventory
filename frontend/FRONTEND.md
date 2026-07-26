# B-Link Frontend — Documentación completa

> Frontend web del sistema de inventario por voz de **Colsubsidio** (B-Link / Phylloinventory).
> Construido con Next.js 14 + TypeScript + Tailwind CSS.

---

## 📋 Tabla de contenidos

1. [Stack y decisiones técnicas](#1-stack-y-decisiones-técnicas)
2. [Setup y arranque](#2-setup-y-arranque)
3. [Estructura del proyecto](#3-estructura-del-proyecto)
4. [Variables de entorno](#4-variables-de-entorno)
5. [Sistema de diseño](#5-sistema-de-diseño)
6. [Rutas y páginas](#6-rutas-y-páginas)
7. [Componentes](#7-componentes)
8. [Cliente API](#8-cliente-api)
9. [Endpoints consumidos](#9-endpoints-consumidos)
10. [Conectar con el backend](#10-conectar-con-el-backend)
11. [Comandos útiles](#11-comandos-útiles)
12. [Pendientes / TODOs](#12-pendientes--todos)

---

## 1. Stack y decisiones técnicas

| Capa | Tecnología | Por qué |
|------|-----------|---------|
| Framework | **Next.js 14** (App Router) | SSR/SSG, BFF nativo, file-based routing |
| Lenguaje | **TypeScript** (strict) | Menos bugs, mejor DX |
| Estilos | **Tailwind CSS 3** | Rapidez, tema custom Colsubsidio |
| HTTP | `fetch` nativo + helper custom | Sin librerías extra (suficiente) |
| Estado local | React hooks (`useState`, `useReducer`) | Suficiente para este MVP |
| Estado servidor | `useEffect` + `useState` (sin TanStack Query todavía) | Simple, sin overhead |
| Audio | MediaRecorder + Web Audio API | Estándar browser, sin libs |
| Iconos | SVG custom inline | Estilo de marca consistente |

**Por qué NO usamos librerías grandes todavía** (TanStack Query, react-hook-form, shadcn, etc.):
- El MVP es chico y se prioriza velocidad de entrega
- Se pueden agregar después sin reescribir (interfaces claras)

---

## 2. Setup y arranque

### Requisitos

- **Node.js 18+** (probado con 24.18)
- **npm** (o pnpm/yarn)
- Backend Phylloinventory corriendo y accesible (default: `http://localhost:8200`)

### Instalación

```bash
cd frontend
npm install
```

### Variables de entorno

Copiar `.env.local.example` a `.env.local`:

```bash
# Windows PowerShell
Copy-Item .env.local.example .env.local

# Linux/Mac
cp .env.local.example .env.local
```

Contenido de `.env.local`:

```bash
NEXT_PUBLIC_API_GATEWAY_URL=http://localhost:8200
NEXT_PUBLIC_API_KEY=
```

> Si el backend tiene `API_KEY` configurada, ponerla acá. Si está vacía (default en dev), no se manda header.

### Dev server

```bash
npm run dev
```

Abrir [http://localhost:3000](http://localhost:3000).

### Build de producción

```bash
npm run build
npm run start
```

---

## 3. Estructura del proyecto

```
frontend/
├── app/                          # Next.js App Router
│   ├── layout.tsx                # Root layout (carga Inter font)
│   ├── globals.css               # Tailwind + animaciones custom
│   ├── page.tsx                  # / — Home (selección de bodega)
│   ├── sesiones/
│   │   └── page.tsx              # /sesiones — Historial
│   └── contar/
│       └── [sesionId]/           # Páginas por sesión
│           ├── page.tsx          # /contar/[id] — Manual (default)
│           ├── voz/
│           │   └── page.tsx      # /contar/[id]/voz — Modo voz (fullscreen)
│           ├── buscar/
│           │   └── page.tsx      # /contar/[id]/buscar — Búsqueda + consulta
│           └── reporte/
│               └── page.tsx      # /contar/[id]/reporte — Reporte final
│
├── components/                   # Componentes reutilizables
│   ├── Logo.tsx                  # Logo Colsubsidio (usa /public/logo.png)
│   └── Icons.tsx                 # Iconos SVG custom (Mic, Keyboard, Search, etc.)
│
├── lib/
│   └── api.ts                    # Cliente HTTP + tipos de error
│
├── public/                       # Assets estáticos
│   ├── logo.png                  # Logo Colsubsidio (header)
│   └── k-mark.png                # K mark (usado en modo Voz)
│
├── scripts/
│   └── center-k.ps1              # PowerShell: auto-centra k-mark.png
│
├── .env.local.example
├── next.config.js
├── package.json
├── postcss.config.js
├── tailwind.config.ts            # Colores custom Colsubsidio
└── tsconfig.json
```

---

## 4. Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `NEXT_PUBLIC_API_GATEWAY_URL` | `http://localhost:8200` | URL del api-gateway de Phylloinventory |
| `NEXT_PUBLIC_API_KEY` | (vacío) | API key para `X-API-Key` header. Vacío = sin auth |

> **⚠️ Producción**: si el frontend es un SPA público, **NO expongas la API_KEY en el bundle**. Usá un BFF (Next.js API routes, Nuxt server routes) o autenticación por cookies.

---

## 5. Sistema de diseño

### Colores (Colsubsidio)

Definidos en `tailwind.config.ts` con nombres `b*`:

| Nombre | Hex | Pantone | Uso |
|--------|-----|---------|-----|
| `bAzul` | `#0067B1` | Pantone 2196 C | Header, links, marca |
| `bAmarillo` | `#FFD000` | Pantone 109 C | CTAs, acentos, progreso |
| `bGris` | `#575756` | Pantone Cool Gray 11 C | Texto secundario, bordes |

Uso en código:

```tsx
className="bg-bAzul text-white"
className="bg-bAmarillo text-gray-900"
className="text-bGris"
```

### Tipografía

- **Fuente**: **Inter** (Google Fonts) — sans-serif moderna, optimizada para UI
- Cargada vía `<link>` en `app/layout.tsx`

```tsx
className="font-sans"  // usa Inter
```

Escala de tamaños:

| Uso | Clase |
|-----|-------|
| H1 (títulos grandes) | `text-4xl font-bold` |
| H2 | `text-2xl font-bold` |
| H3 / Botones | `text-lg font-semibold` |
| Body | `text-base` |
| Labels / Subtle | `text-sm text-gray-600` |
| Mini | `text-xs uppercase tracking-wider` |

### Espaciado

Usa la escala default de Tailwind (`p-4`, `gap-2`, etc.). El contenedor principal es `max-w-2xl mx-auto` (672px).

### Animaciones custom

Definidas en `app/globals.css`:

- `gentle-pulse` — pulso suave (idle)
- `pulse-ring`, `pulse-ring-2`, `pulse-ring-3` — anillos expansivos (grabando)
- `spokes-rotate` — rotación (procesando)
- `dot-bounce-1/2/3` — puntos saltando (respondiendo)
- `pop-in` — entrada con rebote (check)
- `fade-up` — fade in con slide

---

## 6. Rutas y páginas

| Ruta | Página | Auth | Descripción |
|------|--------|------|-------------|
| `/` | Home | No | Selección de bodega, iniciar sesión |
| `/contar/[sesionId]` | Manual | (sesión) | Contar productos (búsqueda + form) |
| `/contar/[sesionId]/voz` | Voz | (sesión) | Modo voz fullscreen (5 estados) |
| `/contar/[sesionId]/buscar` | Buscar | (sesión) | Búsqueda + consulta de productos |
| `/contar/[sesionId]/reporte` | Reporte | (sesión) | Resumen final + alertas |
| `/sesiones` | Historial | No | Lista de sesiones anteriores |
| `/admin` | (pendiente) | — | Dashboard admin (no implementado) |

### `/` — Home

**Componentes**:
- `<Logo />` (header azul)
- Link "Admin" (header)
- Dropdown custom de bodegas
- Botón "Empezar conteo"

**Endpoints**:
- `GET /api/bodegas` (al cargar)
- `POST /api/sesion/iniciar` (al click)

**Comportamiento**:
- Dropdown custom (no `<select>` nativo) para mejor UX mobile
- Click outside para cerrar
- Navega a `/contar/{sesion_id}` al iniciar

### `/contar/[sesionId]` — Manual (default)

**Componentes**:
- Sub-header con info sesión + ⏹ Finalizar
- Barra de progreso (polling cada 2s)
- Tabs: Voz | **Manual*** | Buscar
- Buscador con debounce 300ms
- Lista de últimos registros
- Cards de resultados expandibles (form inline)

**Endpoints**:
- `GET /api/sesion/{id}/estado` (cada 2s)
- `GET /api/catalogo/bodega/{bodega_id}?sesion_id={id}&q=...` (al buscar)
- `POST /api/sesion/registrar-manual` (al registrar)
- `GET /api/pending/{pending_id}` (polling cada 200ms)
- `POST /api/sesion/finalizar` (al click ⏹)

### `/contar/[sesionId]/voz` — Modo Voz

**Estados (state machine)**:
- `idle` — círculo con K, "Toca para hablar"
- `recording` — anillos pulsando + barras de volumen
- `processing` — spokes rotando
- `confirm` — check + texto transcrito
- `responding` — 3 puntos saltando + audio
- `error` — triángulo de error

**Endpoints**:
- `POST /api/audio/transcribir` (multipart, audio webm)
- `POST /api/sesion/registrar-voz` (texto)
- `GET /api/pending/{id}` (polling)
- `POST /api/narrate` (texto natural)
- `POST /api/audio/speak` (TTS, devuelve stream)

**Audio flow**:
1. `MediaRecorder` captura audio del micro (webm)
2. Web Audio API `AnalyserNode` → RMS para barras de volumen
3. POST a `/api/audio/transcribir` → texto
4. POST a `/api/sesion/registrar-voz` → pendiente
5. Polling `/api/pending/{id}` → decisión
6. POST a `/api/narrate` → texto natural para TTS
7. POST a `/api/audio/speak` → stream PCM
8. `<audio>` reproduce el stream

### `/contar/[sesionId]/buscar` — Búsqueda

**Componentes**:
- Buscador grande con autoFocus
- 4 chips de filtro: Todos / Pendientes / Contados / Alertas
- Lista de productos
- Bottom sheet (drawer) de detalle al click

**Endpoints**:
- `GET /api/sesion/{id}/estado` (para bodega_id)
- `GET /api/catalogo/bodega/{bodega_id}?q=...&solo_pendientes=true&sesion_id={id}`

### `/contar/[sesionId]/reporte` — Reporte final

**Componentes**:
- 4 cards de resumen (Total / OK / Alertas / Pendientes)
- Sección "Alertas" colapsable (con botones Confirmar/Rechazar)
- Sección "Contados" colapsable (tabla)
- Sección "Pendientes" colapsable
- Botón "Exportar CSV"

**Endpoints**:
- `GET /api/sesion/{id}/estado` (cards)
- `GET /api/reporte/sospechosos/{id}` (alertas)
- `GET /api/reporte/diferencias/{id}` (contados + no_contados)

**TODO**: el endpoint para Confirmar/Rechazar alertas no existe. Por ahora hace optimistic update.

### `/sesiones` — Historial

**Componentes**:
- Filtros chips: Todas / Activas / Finalizadas
- Cards de sesiones
- Empty state con CTA

**Endpoints (requeridos)**:
- `GET /api/sesiones` — **NO EXISTE en el backend, hay que agregarlo**

---

## 7. Componentes

### `<Logo size="sm" | "md" | "lg" | "xl" href={string|null} />`

Logo de Colsubsidio desde `/public/logo.png`. Tamaños default:

| Size | Alto (px) | Uso |
|------|-----------|-----|
| `sm` | 40 | Header mobile |
| `md` | 56 | Card / login |
| `lg` | 80 | Splash |
| `xl` | 112 | Hero |

Si `href` es `null`, no es clickeable. Si tiene `href`, envuelve en un `<Link>`.

```tsx
<Logo size="sm" href={null} />           // Solo imagen
<Logo size="md" href="/admin" />         // Link a /admin
```

**OnError fallback**: si `/logo.png` no existe, muestra una "C" amarilla en un cuadrado.

### `<MicIcon />` / `<KeyboardIcon />` / `<SearchIcon />` / etc.

Iconos SVG custom en `components/Icons.tsx`. Estilo Colsubsidio (geométricos, amarillos con outline oscuro).

```tsx
import { MicIcon, KeyboardIcon, SearchIcon, StopIcon, DownloadIcon, CloseIcon, ChevronIcon, KMarkIcon } from "@/components/Icons";

<MicIcon size={22} color="#FFD000" strokeColor="#1F2937" />
<StopIcon size={18} color="#1F2937" strokeColor="#1F2937" />  // invertido para fondo amarillo
```

Iconos disponibles:
- `MicIcon` — micrófono (tab Voz)
- `KeyboardIcon` — teclado (tab Manual)
- `SearchIcon` — lupa (tab Buscar)
- `StopIcon` — stop (Finalizar)
- `DownloadIcon` — descargar (Exportar)
- `CloseIcon` — X (cerrar modals)
- `ChevronIcon` — chevron (dropdowns)
- `KMarkIcon` — K de Colsubsidio (logo mark en Voz)

---

## 8. Cliente API

`lib/api.ts`:

```ts
const BASE = process.env.NEXT_PUBLIC_API_GATEWAY_URL || "http://localhost:8200";
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || "";

export class ApiError extends Error {
  status: number;
  body: string;
}

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(opts.headers as Record<string, string> | undefined),
  };
  if (API_KEY) headers["X-API-Key"] = API_KEY;

  const r = await fetch(`${BASE}${path}`, { ...opts, headers });
  if (!r.ok) {
    const body = await r.text();
    throw new ApiError(r.status, body, path);
  }
  return r.json();
}
```

**Uso**:

```ts
import { api, ApiError } from "@/lib/api";

// GET
const bodegas = await api<Bodega[]>("/api/bodegas");

// POST
await api("/api/sesion/iniciar", {
  method: "POST",
  body: JSON.stringify({ bodega_id: 1, iniciada_por: "Carlos" }),
});

// Manejo de error
try {
  await api("/api/sesion/registrar-manual", { ... });
} catch (e) {
  if (e instanceof ApiError && e.status === 404) {
    // endpoint no existe
  }
}
```

**Para uploads de archivos** (no usa el helper, va directo con `fetch`):

```ts
const fd = new FormData();
fd.append("file", blob, "grabacion.webm");
fd.append("language_code", "es");

const res = await fetch(`${GATEWAY}/api/audio/transcribir`, {
  method: "POST",
  body: fd,
  headers: { "X-API-Key": API_KEY },  // NO Content-Type (lo setea fetch)
});
```

---

## 9. Endpoints consumidos

### Bodegas

| Método | Endpoint | Usado en |
|--------|----------|----------|
| GET | `/api/bodegas` | Home, Buscar |

### Sesiones

| Método | Endpoint | Usado en |
|--------|----------|----------|
| POST | `/api/sesion/iniciar` | Home |
| POST | `/api/sesion/finalizar` | Manual, Reporte |
| GET | `/api/sesion/{id}/estado` | Manual, Buscar, Reporte, Sesiones |
| **GET** | **`/api/sesiones`** | **Sesiones (NO EXISTE — agregar al backend)** |

### Conteo (dentro de una sesión)

| Método | Endpoint | Usado en |
|--------|----------|----------|
| GET | `/api/catalogo/bodega/{id}` | Manual, Buscar |
| POST | `/api/sesion/registrar-manual` | Manual |
| POST | `/api/sesion/registrar-voz` | Voz |

### Reportes

| Método | Endpoint | Usado en |
|--------|----------|----------|
| GET | `/api/reporte/diferencias/{id}` | Reporte |
| GET | `/api/reporte/sospechosos/{id}` | Reporte |

### Pendientes (polling)

| Método | Endpoint | Usado en |
|--------|----------|----------|
| GET | `/api/pending/{id}` | Manual, Voz |

### Audio

| Método | Endpoint | Usado en |
|--------|----------|----------|
| POST | `/api/audio/transcribir` | Voz |
| POST | `/api/audio/speak` | Voz |
| GET | `/api/audio/voices` | (no usado todavía) |

### Narrador

| Método | Endpoint | Usado en |
|--------|----------|----------|
| POST | `/api/narrate` | Voz |

---

## 10. Conectar con el backend

### Asumimos que el backend ya tiene CORS abierto

El api-gateway de Phylloininventory ya viene con `CORSMiddleware` configurado y `ALLOWED_ORIGINS` en el `.env`. Para dev con el frontend en `http://localhost:3000`, asegurate que el `.env` del backend tenga:

```bash
ALLOWED_ORIGINS=*
```

(o específicamente `http://localhost:3000`)

### Endpoints que faltan agregar al backend

#### 1. `GET /api/sesiones` (CRÍTICO para la página de Historial)

Editá `services/api-gateway/main.py`:

```python
@app.get("/api/sesiones")
async def list_sesiones():
    """Lista todas las sesiones de conteo, ordenadas por fecha."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                s.id, s.bodega_id, b.nombre as bodega_nombre, 
                s.estado, s.iniciada_por, s.creado_en, s.finalizado_en,
                COUNT(r.id)::int as total_productos,
                SUM(CASE WHEN r.decision_kalman IN ('ACEPTADA', 'CONFIRMADA_MANUAL') THEN 1 ELSE 0 END)::int as contados,
                SUM(CASE WHEN r.decision_kalman = 'SOSPECHOSA' THEN 1 ELSE 0 END)::int as alertas
            FROM sesiones_conteo s
            LEFT JOIN bodegas b ON s.bodega_id = b.id
            LEFT JOIN registros_conteo r ON s.id = r.sesion_id
            GROUP BY s.id, b.nombre, s.estado, s.iniciada_por, s.creado_en, s.finalizado_en
            ORDER BY s.creado_en DESC
        """)
        return [dict(r) for r in rows]
```

**Response esperado** (lo que el frontend consume):

```json
[
  {
    "id": 5,
    "bodega_id": 1,
    "bodega_nombre": "Bodega Cocina",
    "estado": "finalizada",
    "iniciada_por": "Carlos",
    "creado_en": "2026-07-25T18:00:00Z",
    "finalizado_en": "2026-07-25T19:30:00Z",
    "total_productos": 287,
    "contados": 280,
    "alertas": 5
  }
]
```

#### 2. `POST /api/pending/{id}/confirmar` y `POST /api/pending/{id}/rechazar` (para botones de alertas en Reporte)

Hoy los botones del Reporte hacen optimistic update pero no llaman a nada. El backend necesita endpoints para confirmar/rechazar alertas SOSPECHOSA. Formato sugerido:

```python
@app.post("/api/pending/{pending_id}/confirmar")
async def confirmar_pending(pending_id: int):
    # Llama a la DB function confirmar_movimiento(pending_id, true)
    ...

@app.post("/api/pending/{pending_id}/rechazar")
async def rechazar_pending(pending_id: int):
    # Llama a la DB function confirmar_movimiento(pending_id, false)
    ...
```

### Auth opcional

Si el backend tiene `API_KEY` configurada, hay que poner la misma en el `.env.local` del frontend. En dev, dejar vacía.

---

## 11. Comandos útiles

```bash
# Dev server (hot reload)
npm run dev

# Build de producción
npm run build
npm run start

# Lint
npm run lint

# Limpiar caché de Next
rm -rf .next

# Auto-centrar k-mark.png (PowerShell)
powershell -ExecutionPolicy Bypass -File scripts/center-k.ps1
```

### Troubleshooting

| Problema | Solución |
|----------|----------|
| `npm: command not found` | Usar `npm.cmd` o cambiar Execution Policy |
| Página en blanco | F12 → Console, ver error |
| Cambios no se ven | `Ctrl + Shift + R` (hard refresh) |
| Logo no aparece | Verificar que `public/logo.png` existe |
| Error de CORS | Backend: `ALLOWED_ORIGINS=*` en `.env` |
| API 401 | `API_KEY` del backend no coincide con el del frontend |

---

## 12. Pendientes / TODOs

### Backend (necesario para funcionalidad completa)

- [ ] **`GET /api/sesiones`** — listar sesiones para `/sesiones`
- [ ] **`POST /api/pending/{id}/confirmar`** — confirmar alertas en Reporte
- [ ] **`POST /api/pending/{id}/rechazar`** — rechazar alertas en Reporte
- [ ] **Voice list**: `GET /api/audio/voices` no se está usando, podría alimentar un selector de voz

### Frontend (mejoras pendientes)

- [ ] **Admin module** (`/admin/*`) — dashboard, métricas, sesiones, inventario
- [ ] **Auth de usuario** — actualmente no hay login, solo el header dice "Admin"
- [ ] **Persistencia de sesión activa** — si recargás, perdés el sesionId
- [ ] **Error boundaries** a nivel de página
- [ ] **Loading states** más pulidos (skeletons)
- [ ] **Optimistic updates** con rollback en errores
- [ ] **TanStack Query** para cache, refetch, optimistic updates
- [ ] **PWA** — service worker, manifest, installable
- [ ] **i18n** — todo hardcoded en español
- [ ] **Tests** — unit tests (Vitest), e2e (Playwright)
- [ ] **Manejo de imagen rota en `Logo`** — ya hay fallback, pero pulir
- [ ] **Modal de confirmación SOSPECHOSA** en Manual (cuando Kalman FALLA)

### Mejoras de UX

- [ ] **Auto-stop por silencio** en modo Voz (ahora solo para con click)
- [ ] **Búsqueda fuzzy** en el dropdown de bodegas
- [ ] **Búsqueda con teclado** en Manual (autocomplete tipo combobox)
- [ ] **Atajos de teclado** globales (ej: Esc para volver)
- [ ] **Toast notifications** globales (en vez de alerts)
- [ ] **Animación de transición** entre páginas

### Producción

- [ ] **BFF** (Backend-for-Frontend) para no exponer `API_KEY` en el bundle
- [ ] **PWA completa** con offline support
- [ ] **CI/CD** pipeline
- [ ] **Analytics** (eventos de uso, errores)
- [ ] **Error reporting** (Sentry, etc.)
- [ ] **Performance monitoring** (Web Vitals)

---

## 📞 Contacto

- **Cliente**: Colsubsidio
- **Sistema**: Phylloinventory / B-Link
- **Mantenedor**: [tu equipo]
- **Brand**: colores oficiales Pantone 109 C, 2196 C, Cool Gray 11 C

---

> **Última actualización**: Julio 2026
> **Versión del frontend**: 0.1.0 (MVP)
> **Versión del backend esperada**: Phylloinventory Fase 9+
