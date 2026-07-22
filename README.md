# 🌵 Cactus Inventory Demo — Filtro de Kalman + Needle

Demo de manejo de inventarios con **Filtro de Kalman** usando
**[Needle](https://github.com/cactus-compute/needle)** (modelo de 26M params para function calling).

## Arquitectura

```
📝 CLI (Python/Rich)
     │
     ▼ HTTP :8081
┌─────────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Needle Docker  │     │  Tool Server     │     │  PostgreSQL  │
│  (26M params)   │────▶│  (FastAPI :8000) │────▶│  Docker :5432│
│  llama al tool  │     │  ejecuta accion  │     │  Kalman TRG  │
└─────────────────┘     └──────────────────┘     └──────────────┘
```

## Requisitos

- **Docker Desktop** (Windows/Mac/Linux)
- **Python 3.10+**
- ~2 GB de espacio para la imagen Docker de Needle

## Instalacion y ejecucion

```bash
cd storage

# 1. Entorno virtual (opcional)
python -m venv .venv
.venv\Scripts\activate   # Windows

# 2. Instalar dependencias del CLI y tool server
pip install -r requirements.txt

# 3. Levantar PostgreSQL + Needle (Docker)
docker-compose up -d

# 4. Esperar a que Needle descargue el modelo (~30s primer arranque)
docker logs -f cactus_needle_server
# Cuando veas "Needle model loaded" -> Ctrl+C

# 5. Iniciar el tool server (en una terminal aparte)
python -m src.tool_server

# 6. Iniciar la CLI interactiva (en otra terminal)
python -m src.cli
```

## Uso

```
📝 > agrega 4 papas
  Needle esta pensando...
  🔧 [agregar_inventario] {"producto": "papa", "cantidad": 4}
  V Movimiento ACEPTADO por Kalman. papa: 50 -> 54

📝 > agrega 200 papas de una
  Needle esta pensando...
  🔧 [agregar_inventario] {"producto": "papa", "cantidad": 200}
  ALERTA — Filtro de Kalman
    Residual: 200s | Riesgo: CRITICO

⚠️  ¿Confirmas? (si/no) > dale
  V Movimiento confirmado. Stock actualizado a 254

📝 > investiga si hay algo raro en el inventario
  Needle esta pensando...
  🔧 [investigar_sospechosos] {}
  MAYOR SOSPECHOSO: #2 | papa entrada 200 | 75.7s
```

## Modo de emergencia (sin Needle)

Si Needle no esta disponible, puedes usar el parser regex:

```bash
python -m src.cli --fallback
```

## Estructura del proyecto

```
storage/
├── docker-compose.yml         # PostgreSQL + Needle
├── Dockerfile.needle          # Imagen Docker para Needle
├── serve_needle.py            # Servidor HTTP de Needle (:8081)
├── db/init.sql                # Schema + triggers Kalman
├── src/
│   ├── cli.py                 # CLI interactiva (Rich)
│   ├── agent.py               # NeedleHTTPAgent + CactusAgent + fallback
│   ├── tool_server.py         # FastAPI tool server (:8000)
│   ├── tools.json             # Definiciones de tools
│   └── db_client.py           # Cliente PostgreSQL
└── requirements.txt
```

## Filtro de Kalman — Como funciona

Cada producto tiene un estado estimado `(μ, σ²)`:

1. **Prediccion**: `P_pred = P + Q`
2. **Medicion**: `z = stock_actual + delta_reportado`
3. **Innovacion**: `y = z - μ`
4. **Decision**: Si `|y| > 2.0 * sqrt(P_pred + R)` → SOSPECHOSO
5. **Actualizacion**: `K = P_pred / (P_pred + R)`, `μ_new = μ + K*y`, `P_new = (1-K)*P_pred`

## Licencia

MIT
