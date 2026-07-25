"""CLI cliente delgado. Habla con api-gateway (HTTP) y voice-service (WebSocket).

Comandos:
  texto libre               -> POST /query al api-gateway
  voz                       -> graba microfono, transcribe via voice-service WS
  inventario                -> GET /inventory
  sospechosos [producto]    -> GET /sospechosos
  salir / exit / q          -> cierra
  ayuda / help              -> muestra el banner
  limpiar / clear           -> limpia pantalla
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from typing import Any

import httpx
from rich.align import Align
from rich import box as _box_module
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

# Compat: rich>=13 usa `box` (instancia Box), rich>=15 usa Box en `box`
try:
    box = _box_module.box  # type: ignore[attr-defined]
except AttributeError:
    box = _box_module  # type: ignore[assignment]

from . import api_client
from . import tts_client
from .voice_client import record_and_transcribe, available as voice_available

console = Console()
SESSION_ID = os.getenv("CLI_SESSION_ID", f"cli-{uuid.uuid4().hex[:8]}")


# =====================================================================
#  Estado de la sesion
# =====================================================================

class SessionState:
    def __init__(self):
        self.last_sospechoso: dict | None = None  # para enriquecer contexto


# =====================================================================
#  Banner / render
# =====================================================================

def show_banner(backend: str, voice_ok: bool = False, config: dict | None = None) -> None:
    if config is None:
        config = {}
    cfg = config.get("config", config) if isinstance(config.get("config"), dict) else config
    llm = cfg.get("llm", backend)
    stt = cfg.get("stt", "?")
    tts = cfg.get("tts", "?")
    cloud = cfg.get("cloud_enabled", False)
    cloud_tag = " [bold magenta]CLOUD[/bold magenta]" if cloud else ""

    voice_line = (
        "[green]Voz: OK (microfono disponible)[/green]"
        if voice_ok
        else "[yellow]Voz: sounddevice no instalado (pip install sounddevice)[/yellow]"
    )
    backends_line = (
        f"[dim]LLM: [bold cyan]{llm}[/bold cyan]  "
        f"STT: [bold cyan]{stt}[/bold cyan]  "
        f"TTS: [bold cyan]{tts}[/bold cyan][/dim]{cloud_tag}"
    )
    panel = Panel(
        Align.center(
            Text.from_markup(
                "[bold green]Cactus Inventory - Microservicios[/bold green]\n"
                f"Session: [dim]{SESSION_ID}[/dim]\n"
                f"{backends_line}\n"
                f"[dim]{voice_line}[/dim]\n"
                "[dim]Kalman evaluado por worker Go - Cola en PostgreSQL[/dim]\n\n"
                "[yellow]Comandos:[/yellow]\n"
                "  texto libre             -> enviar al LLM\n"
                "  voz                     -> dictar por microfono\n"
                "  tts <texto>             -> probar TTS (sintetiza y reproduce)\n"
                "  cloud on|off|status     -> toggle cloud (Eleven Labs + OpenRouter)\n"
                "  voices                  -> listar voces Eleven Labs disponibles\n"
                "  inventario              -> ver catalogo (1 fila x producto)\n"
                "  inventario <bodega>     -> stock por bodega\n"
                "  sospechosos [producto]  -> auditoria Kalman\n"
                "  salir / exit / q        -> cerrar\n"
                "  ayuda / help            -> mostrar este banner\n"
                "  limpiar / clear         -> limpiar pantalla"
            ),
            vertical="middle",
        ),
        border_style="green",
        box=box.ROUNDED,
    )
    console.print(panel)


def show_inventory(rows: list[dict]) -> None:
    table = Table(title="Inventario Actual", box=box.SIMPLE_HEAVY, border_style="cyan")
    table.add_column("Producto", style="bold")
    table.add_column("Stock", justify="right")
    table.add_column("Kalman mu", justify="right")
    table.add_column("s2", justify="right")
    for r in rows:
        if "stock_actual" not in r:
            r = {**r, "stock_actual": "-", "media_kalman": 0, "varianza_kalman": 0}
        table.add_row(
            r["nombre"],
            str(r["stock_actual"]),
            f"{r['media_kalman']:.1f}",
            f"{r['varianza_kalman']:.1f}",
        )
    console.print(table)


def show_catalog(rows: list[dict]) -> None:
    table = Table(title="Catalogo de Productos", box=box.SIMPLE_HEAVY, border_style="cyan")
    table.add_column("Producto", style="bold")
    table.add_column("Unidad")
    table.add_column("Codigo", justify="right")
    for r in rows:
        table.add_row(
            r["nombre"],
            r.get("unidad", "-"),
            str(r.get("codigo_articulo") or "-"),
        )
    console.print(table)


def risk_label(sigma: float) -> str:
    if sigma > 30:
        return "CRITICO"
    if sigma > 10:
        return "ALTO"
    if sigma > 3:
        return "MEDIO"
    return "BAJO"


RISK_COLORS = {"CRITICO": "red", "ALTO": "yellow", "MEDIO": "dim cyan", "BAJO": "green"}


# =====================================================================
#  Narracion TTS (kokoro-service). Fire-and-forget: nunca bloquea el loop.
# =====================================================================

def _narrate(phrase: str) -> None:
    """Lanza la reproduccion de TTS en background. Si el servicio no esta
    disponible o sounddevice falla, simplemente no suena — el flujo sigue."""
    if not phrase:
        return
    #  create_task sin await: el TTS se reproduce en su thread.
    asyncio.create_task(tts_client.speak(phrase))


def _narrate_aceptada(tool_name: str, args: dict, inv: dict | list | None) -> None:
    producto = args.get("producto") or "el producto"
    cantidad = args.get("cantidad")
    unidad = args.get("unidad") or ""

    if isinstance(inv, dict) and "stock_actual" in inv:
        stock = inv["stock_actual"]
        bodega = inv.get("bodega") or "la bodega"
    elif isinstance(inv, list) and inv:
        stock = inv[0].get("stock_actual", "?")
        bodega = inv[0].get("bodega", "la bodega")
    else:
        stock = "?"
        bodega = "la bodega"

    if tool_name == "agregar_inventario":
        _narrate(f"Se agregaron {cantidad} {unidad} de {producto}. Stock actual: {stock} {unidad}, en {bodega}.")
    elif tool_name == "remover_inventario":
        _narrate(f"Se removieron {cantidad} {unidad} de {producto}. Stock actual: {stock} {unidad}, en {bodega}.")


def _narrate_sospechosa(args: dict, puntaje: float, residual: float) -> None:
    producto = args.get("producto") or "el producto"
    cantidad = args.get("cantidad")
    unidad = args.get("unidad") or ""
    tipo = "ingreso" if args.get("tool_name") == "agregar_inventario" else "salida"
    _narrate(
        f"Atención. Se detectó un movimiento sospechoso: {tipo} de {cantidad} {unidad} de {producto}. "
        f"Riesgo de {puntaje:.1f} sigmas. Por favor confirma con sí o no."
    )


def _narrate_confirmada() -> None:
    _narrate("Movimiento confirmado. El stock fue actualizado.")


def _narrate_rechazada() -> None:
    _narrate("Movimiento rechazado. El stock no fue modificado.")


def _narrate_consulta(inv) -> None:
    if isinstance(inv, dict) and "stock_actual" in inv:
        _narrate(
            f"Hay {inv['stock_actual']} {inv.get('unidad', '')} de {inv.get('nombre', 'ese producto')} "
            f"en {inv.get('bodega', 'la bodega')}."
        )
    elif isinstance(inv, list) and inv:
        if len(inv) == 1:
            r = inv[0]
            _narrate(f"Hay {r['stock_actual']} {r.get('unidad', '')} de {r['nombre']} en {r.get('bodega', 'la bodega')}.")
        else:
            total = sum(float(r.get("stock_actual") or 0) for r in inv)
            partes = ", ".join(f"{r.get('stock_actual')} en {r.get('bodega')}" for r in inv[:3])
            _narrate(f"Hay un total de {total} unidades. Encontre stock en: {partes}.")


def _narrate_sospechosos(rows: list[dict]) -> None:
    if not rows:
        _narrate("No hay movimientos sospechosos en la auditoria.")
        return
    top = max(rows, key=lambda r: r["puntaje_riesgo"])
    _narrate(
        f"Encontre {len(rows)} movimientos sospechosos. "
        f"El mas grave: {top['producto_nombre']}, {top['tipo']} de {top['cantidad_reportada']}, "
        f"con un riesgo de {top['puntaje_riesgo']:.1f} sigmas."
    )


def _narrate_invalid(tool_name: str, args: dict) -> None:
    """Caso 'no se encolo' — el LLM devolvio la tool pero el producto/cantidad
    no son validos, asi que la CLI la descarto y debe avisar al usuario."""
    prod = args.get("producto") or ""
    cant = args.get("cantidad")
    if tool_name == "confirmar_movimiento":
        _narrate("No entendi la confirmacion. Responde si o no.")
    elif not prod:
        accion = "agregar" if tool_name == "agregar_inventario" else "remover"
        _narrate(
            f"No se que producto quieres {accion}. "
            f"Dime algo como: {accion} cinco kilos de papa, o cuanto hay de tomate."
        )
    elif cant is None or float(cant) <= 0:
        _narrate(f"La cantidad no es valida. Cuanto quieres {tool_name.replace('_', ' ')} de {prod}?")
    else:
        _narrate(f"No se pudo procesar la operacion sobre {prod}.")


def _narrate_no_action() -> None:
    """Caso 'tool_calls vacio' — el LLM no decidio nada util."""
    _narrate(
        "No entendi la instruccion. Prueba con agregar cinco kilos de papa, "
        "cuanto hay de tomate, o hay algo sospechoso."
    )


def show_sospechosos(rows: list[dict]) -> None:
    if not rows:
        console.print("  [green]No hay movimientos sospechosos.[/green]")
        return
    table = Table(title="Auditoria Kalman — Sospechosos", box=box.SIMPLE_HEAVY, border_style="red")
    table.add_column("#", justify="right")
    table.add_column("Producto")
    table.add_column("Tipo")
    table.add_column("Cant.", justify="right")
    table.add_column("Riesgo (s)", justify="right")
    table.add_column("Nivel")
    table.add_column("Fecha")
    for s in rows:
        nivel = risk_label(s["puntaje_riesgo"])
        color = RISK_COLORS.get(nivel, "white")
        table.add_row(
            str(s["movimiento_id"]),
            s["producto_nombre"],
            s["tipo"],
            str(s["cantidad_reportada"]),
            f"{s['puntaje_riesgo']:.1f}",
            f"[{color}]{nivel}[/{color}]",
            str(s["fecha"])[:19] if s.get("fecha") else "-",
        )
    console.print(table)
    top = max(rows, key=lambda r: r["puntaje_riesgo"])
    console.print(
        f"  [bold red]MAYOR SOSPECHOSO:[/bold red] #{top['movimiento_id']} | "
        f"{top['producto_nombre']} {top['tipo']} {top['cantidad_reportada']} | "
        f"{top['puntaje_riesgo']:.1f}s"
    )


# =====================================================================
#  Handlers
# =====================================================================

async def handle_query(state: SessionState, text: str) -> None:
    with Status("[dim]Pensando...[/dim]", console=console) as status:
        try:
            resp = await api_client.query(text, SESSION_ID, state.last_sospechoso)
        except httpx.HTTPError as e:
            status.stop()
            console.print(f"[red]Error contactando api-gateway: {e}[/red]")
            return
        status.stop()

    if resp.get("raw_output"):
        console.print(f"  [dim]LLM: {resp['raw_output'][:120]}[/dim]")

    #  Indicador de backend y fallback
    backend = resp.get("backend")
    requested = resp.get("backend_requested")
    if backend and requested:
        if resp.get("fallback_used"):
            console.print(f"  [yellow]i backend=[/yellow][cyan]{backend}[/cyan][yellow] (solicitado {requested}, fallback por error)[/yellow]")
        elif backend != requested:
            console.print(f"  [dim]backend=[/dim][cyan]{backend}[/cyan][dim] (override activo)[/dim]")

    tool_calls = resp.get("tool_calls", [])
    if not tool_calls:
        if state.last_sospechoso:
            console.print("[yellow]Tienes una alerta pendiente. Responde 'si' o 'no'.[/yellow]")
            _narrate("Tienes una alerta pendiente. Responde si o no.")
        else:
            console.print(
                "[yellow]No detecte ninguna accion. Prueba:[/yellow] "
                "'agrega 4 papas' / 'cuanto hay de tomate' / 'hay algo raro'"
            )
            _narrate_no_action()
        return

    for call in tool_calls:
        name = call.get("name")
        args = call.get("arguments", {})
        console.print(f"  [cyan][tool] {name}[/cyan] {args}")

    #  Procesamos pendientes en paralelo
    pending = resp.get("pending", [])

    #  Aviso si una escritura/confirmacion no llego a la cola (p.ej. producto
    #  invalido). Antes fallaba en silencio y "no pasaba nada".
    enqueued = {p["tool_name"] for p in pending if p.get("pending_id")}
    for call in tool_calls:
        n = call.get("name")
        if n in ("agregar_inventario", "remover_inventario", "confirmar_movimiento") and n not in enqueued:
            console.print(
                f"  [yellow]i '{n}' no se encolo — revisa producto/cantidad "
                f"(validos: papa, cebolla, tomate, zanahoria, ajo)[/yellow]"
            )
            _narrate_invalid(n, call.get("arguments") or {})
    write_actions = [p for p in pending if p["tool_name"] in ("agregar_inventario", "remover_inventario")]
    confirm_actions = [p for p in pending if p["tool_name"] == "confirmar_movimiento"]
    read_actions = [p for p in pending if p["tool_name"] in ("consultar_inventario", "investigar_sospechosos")]

    #  Reads
    for ra in read_actions:
        if ra["tool_name"] == "consultar_inventario":
            prod = ra["arguments"].get("producto")
            try:
                inv = await api_client.get_inventory(prod)
                if isinstance(inv, list):
                    show_inventory(inv)
                else:
                    console.print(f"  [green]{inv['nombre']}: {inv['stock_actual']} unidades[/green]")
                _narrate_consulta(inv)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
        elif ra["tool_name"] == "investigar_sospechosos":
            try:
                rows = await api_client.get_sospechosos(ra["arguments"].get("producto"))
                show_sospechosos(rows)
                _narrate_sospechosos(rows)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

    #  Writes + confirmations: poll hasta que el worker resuelva
    for p in write_actions + confirm_actions:
        pid = p["pending_id"]
        try:
            row = await api_client.poll_until_resolved(pid)
        except Exception as e:
            console.print(f"[red]Error polling pending {pid}: {e}[/red]")
            continue
        await _render_pending(state, p, row)


async def _render_pending(state: SessionState, p: dict, row: dict) -> None:
    pid = p["pending_id"]
    name = p["tool_name"]
    args = p["arguments"]
    status = row.get("status")
    decision = row.get("decision")
    residual = row.get("residual") or 0
    umbral = row.get("umbral") or 0
    puntaje = abs(residual) / umbral if umbral else 0

    if status == "ACEPTADA":
        #  Recuperar info de stock para el mensaje
        try:
            inv = await api_client.get_inventory(args.get("producto"))
            if isinstance(inv, dict) and "stock_actual" in inv:
                console.print(
                    f"  [bold green]V {name} ACEPTADO[/bold green] · "
                    f"{inv['nombre']} stock={inv['stock_actual']} · residual={residual:.1f}s"
                )
                state.last_sospechoso = None
                _narrate_aceptada(name, args, inv)
                return
        except Exception:
            pass
        console.print(f"  [bold green]V {name} ACEPTADO[/bold green] (pending #{pid})")
        state.last_sospechoso = None
        _narrate_aceptada(name, args, None)
        return

    if status == "CONFIRMADA_MANUAL":
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            import json as _json
            try:
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        result = payload.get("result", "Movimiento confirmado")
        console.print(f"  [bold green]V {result}[/bold green]")
        state.last_sospechoso = None
        _narrate_confirmada()
        return

    if status == "RECHAZADA":
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            import json as _json
            try:
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        result = payload.get("result", "Movimiento rechazado")
        console.print(f"  [bold red]X {result}[/bold red]")
        state.last_sospechoso = None
        _narrate_rechazada()
        return

    if status == "SOSPECHOSA":
        state.last_sospechoso = {
            "pending_id": pid,
            "producto": args.get("producto"),
            "cantidad": args.get("cantidad"),
            "tipo": "entrada" if name == "agregar_inventario" else "salida",
            "residual": residual,
            "puntaje_riesgo": puntaje,
        }
        _narrate_sospechosa({**args, "tool_name": name}, puntaje, residual)
        nivel = risk_label(puntaje)
        color = RISK_COLORS.get(nivel, "white")
        panel = Panel(
            Text.from_markup(
                f"[bold yellow]ALERTA — Filtro de Kalman[/bold yellow]\n\n"
                f"  Producto:      [cyan]{args.get('producto')}[/cyan]\n"
                f"  Cantidad:      [cyan]{args.get('cantidad')}[/cyan]\n"
                f"  Pending ID:    [cyan]{pid}[/cyan]\n"
                f"  Residual:      [yellow]{residual:.1f}s[/yellow]\n"
                f"  Riesgo:        [{color}]{nivel} ({puntaje:.1f}s)[/{color}]\n\n"
                f"[dim]El valor reportado se desvia del patron esperado.[/dim]\n"
                f"[bold]Responde:[/bold]  [green]'si' / 'dale'[/green]   [red]'no' / 'cancela'[/red]"
            ),
            border_style="yellow",
            box=box.ROUNDED,
        )
        console.print(panel)
        return

    if status == "TIMEOUT":
        console.print(f"  [yellow]i pending #{pid} sigue PENDIENTE tras timeout. Sigue esperando al worker.[/yellow]")
        return

    if status == "NOT_FOUND":
        console.print(f"  [red]pending #{pid} no existe[/red]")
        return

    console.print(f"  [yellow]i pending #{pid} status={status}[/yellow]")


# =====================================================================
#  Voz
# =====================================================================

async def handle_voice(state: SessionState) -> None:
    if not voice_available():
        console.print("[red]Falta sounddevice para capturar audio.[/red]\n  pip install sounddevice")
        return

    #  Pre-check: que el gateway este vivo (el gateway ya hace el routing al backend activo)
    try:
        h = await api_client.health()
    except Exception as e:
        console.print(f"[red]api-gateway no responde: {e}[/red]")
        return
    cfg = h.get("config", {})
    stt = cfg.get("stt", "?")
    console.print(
        f"[dim]STT activo: [cyan]{stt}[/cyan]  "
        f"({'cloud' if cfg.get('cloud_enabled') else 'local'})[/dim]"
    )

    console.print("[bold magenta]Grabando... presiona Enter para detener[/bold magenta]")
    stop = asyncio.Event()

    def _wait_enter():
        try:
            input()
        except EOFError:
            pass
        stop.set()

    import threading
    enter_thread = threading.Thread(target=_wait_enter, daemon=True)
    enter_thread.start()

    try:
        text: str | None = await record_and_transcribe(stop)
    except Exception as e:
        console.print(f"[red]Error de voz: {e}[/red]")
        console.print("  [dim]Tip: puedes seguir escribiendo texto normalmente.[/dim]")
        text = None
    finally:
        stop.set()
        if enter_thread.is_alive():
            console.print("[dim](presiona Enter para continuar)[/dim]")
            await asyncio.get_event_loop().run_in_executor(None, enter_thread.join)
        print()  # limpia el [voz parcial]

    if text is None:
        return
    if not text:
        console.print("[yellow]No se escucho nada util.[/yellow]")
        return

    console.print(f"  [dim]Escuchado:[/dim] [bold magenta]{text}[/bold magenta]")
    await handle_query(state, text)


# =====================================================================
#  Cloud toggle / voices
# =====================================================================

async def handle_cloud(state: SessionState, args: str) -> None:
    """`cloud on|off|status` o `cloud stt=elevenlabs tts=elevenlabs` etc."""
    args = args.strip()
    if not args or args == "status":
        try:
            cfg = await api_client.get_config()
        except Exception as e:
            console.print(f"[red]No se pudo leer config: {e}[/red]")
            return
        _print_config(cfg)
        return
    if args in ("on", "off"):
        try:
            cfg = await api_client.set_config(cloud_enabled=(args == "on"))
        except Exception as e:
            console.print(f"[red]Toggle fallo: {e}[/red]")
            return
        console.print(
            f"[bold green]cloud {'ON' if cfg.get('cloud_enabled') else 'OFF'}[/bold green]  "
            f"LLM=[cyan]{cfg.get('llm')}[/cyan]  STT=[cyan]{cfg.get('stt')}[/cyan]  TTS=[cyan]{cfg.get('tts')}[/cyan]"
        )
        if cfg.get("fallback_used") is False and cfg.get("cloud_enabled"):
            console.print("  [dim]Tip: el gateway probara cloud primero; si falla, fallback a local automatico.[/dim]")
        return
    #  Override por-backend: cloud stt=elevenlabs tts=kokoro llm=auto
    overrides: dict = {}
    for token in args.split():
        if "=" not in token:
            console.print(f"[yellow]ignoro token '{token}' (esperaba k=v)[/yellow]")
            continue
        k, v = token.split("=", 1)
        k = k.strip().lower()
        v = v.strip().lower()
        if k not in ("llm", "stt", "tts"):
            console.print(f"[yellow]backend '{k}' no soportado (usa llm/stt/tts)[/yellow]")
            continue
        overrides[k] = v
    if not overrides:
        console.print("[yellow]uso: cloud on|off|status | cloud llm=needle stt=whisper tts=kokoro[/yellow]")
        return
    try:
        cfg = await api_client.set_config(**overrides)
    except Exception as e:
        console.print(f"[red]set_config fallo: {e}[/red]")
        return
    _print_config(cfg)


def _print_config(cfg: dict) -> None:
    cloud = cfg.get("cloud_enabled", False)
    color = "magenta" if cloud else "green"
    console.print(
        f"[{color}]cloud_enabled = {cloud}[/{color}]\n"
        f"  LLM = [cyan]{cfg.get('llm')}[/cyan]  (override: {cfg.get('llm_override') or 'auto'})\n"
        f"  STT = [cyan]{cfg.get('stt')}[/cyan]  (override: {cfg.get('stt_override') or 'auto'})\n"
        f"  TTS = [cyan]{cfg.get('tts')}[/cyan]  (override: {cfg.get('tts_override') or 'auto'})\n"
        f"  [dim]defaults (env): llm={cfg.get('defaults', {}).get('llm')}, "
        f"stt={cfg.get('defaults', {}).get('stt')}, tts={cfg.get('defaults', {}).get('tts')}[/dim]"
    )


async def handle_voices(state: SessionState, args: str) -> None:
    """`voices` -> lista las voces disponibles del backend TTS activo."""
    try:
        data = await api_client.list_voices()
    except Exception as e:
        console.print(f"[red]No se pudo listar voces: {e}[/red]")
        return
    backend = data.get("backend", "?")
    default_vid = data.get("default_voice_id", "?")
    voices = data.get("voices", [])
    if not voices:
        console.print("[yellow]No hay voces disponibles.[/yellow]")
        return
    table = Table(title=f"Voces disponibles ({backend})", box=box.SIMPLE_HEAVY, border_style="cyan")
    table.add_column("Voice ID", style="bold", no_wrap=True)
    table.add_column("Nombre")
    table.add_column("Categoria")
    table.add_column("Labels")
    for v in voices:
        is_default = v.get("voice_id") == default_vid
        name = v.get("name", "?")
        if is_default:
            name = f"{name}  [green](default)[/green]"
        labels = v.get("labels") or {}
        labels_str = ", ".join(f"{k}={val}" for k, val in list(labels.items())[:4])
        table.add_row(
            v.get("voice_id", "?"),
            name,
            v.get("category", "-") or "-",
            labels_str or "-",
        )
    console.print(table)
    console.print(f"  [dim]Para usar otra voz en TTS: tts <texto>  (por ahora se usa la default; selector en PWA)[/dim]")


# =====================================================================
#  Loop principal
# =====================================================================

HELP = "ayuda", "help", "?"
CLEAR = "limpiar", "clear"
QUIT = "salir", "exit", "quit", "q"


async def main_async(args: argparse.Namespace) -> int:
    #  Health check inicial
    try:
        h = await api_client.health()
        backend = h.get("config", {}).get("llm") or h.get("backend", "?")
        config = h.get("config", {})
    except Exception as e:
        console.print(f"[red]No se pudo conectar al api-gateway: {e}[/red]")
        console.print("[yellow]Asegurate de que docker compose este arriba.[/yellow]")
        return 1

    #  Estado del microfono local
    from .voice_client import available as voice_available_lib
    voice_ok = voice_available_lib()

    show_banner(backend, voice_ok=voice_ok, config=config)
    try:
        cat = await api_client.get_catalog()
        if cat:
            console.print()
            show_catalog(cat)
            console.print()
    except Exception:
        pass

    #  Estado del TTS (checkeamos kokoro directo solo si esta activo; si no,
    #  el banner ya muestra TTS=elevenlabs).
    if os.getenv("DISABLE_TTS", "").lower() not in ("1", "true", "yes"):
        if config.get("tts") == "kokoro":
            tts_ok = await tts_client.is_available()
            if tts_ok:
                console.print("[green]TTS: kokoro-service listo (las respuestas sonaran)[/green]")
            else:
                console.print(
                    "[yellow]TTS: kokoro-service no responde en "
                    f"{os.getenv('KOKORO_URL', 'http://127.0.0.1:8205')}[/yellow]\n"
                    "  [dim]Tip: arranca con: docker compose up -d kokoro-service[/dim]"
                )
        else:
            console.print("[green]TTS: elevenlabs activo (cloud)[/green]")
    else:
        console.print("[dim]TTS: desactivado por env (DISABLE_TTS=1)[/dim]")

    state = SessionState()
    while True:
        prompt = (
            "[bold yellow]Confirma? (si/no) > [/bold yellow]"
            if state.last_sospechoso
            else "[bold cyan]> [/bold cyan]"
        )
        try:
            user_input = await asyncio.get_event_loop().run_in_executor(None, lambda: console.input(prompt).strip())
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Adios.[/dim]")
            break

        if not user_input:
            continue
        low = user_input.lower()

        if low in QUIT:
            console.print("[dim]Adios.[/dim]")
            break
        if low in HELP:
            show_banner(backend, voice_ok=voice_ok, config=config)
            continue
        if low in CLEAR:
            console.clear()
            show_banner(backend, voice_ok=voice_ok, config=config)
            continue
        if low == "voz":
            await handle_voice(state)
            console.print()
            continue
        if low.startswith("cloud"):
            await handle_cloud(state, user_input[4:].strip())
            console.print()
            continue
        if low == "voices":
            await handle_voices(state, "")
            console.print()
            continue
        if low.startswith("tts "):
            #  tts <texto> — el gateway elige el backend (kokoro o elevenlabs)
            phrase = user_input[4:].strip()
            if not phrase:
                console.print("[yellow]uso: tts <texto a pronunciar>[/yellow]")
            else:
                try:
                    res = await api_client.speak_remote(phrase)
                except Exception as e:
                    console.print(f"[red]TTS fallo: {e}[/red]")
                    console.print("  [dim]Tip: 'cloud status' para ver que backend esta activo.[/dim]")
                    console.print()
                    continue
                #  Reproducir el audio recibido (PCM int16)
                backend = res.get("backend", "?")
                fallback = res.get("fallback_used", False)
                tag = f"[cyan]{backend}[/cyan]"
                if fallback:
                    tag += " [yellow](fallback)[/yellow]"
                console.print(f"  TTS backend: {tag}  sample_rate={res.get('sample_rate')}Hz")
                await tts_client.play_pcm(
                    res["audio"],
                    sample_rate=res.get("sample_rate", 24000),
                )
            console.print()
            continue
        if low == "tts":
            console.print("[yellow]uso: tts <texto a pronunciar>[/yellow]")
            console.print()
            continue
        if low == "inventario":
            try:
                cat = await api_client.get_catalog()
                show_catalog(cat)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
            console.print()
            continue
        if low.startswith("inventario "):
            parts = user_input.split(maxsplit=1)
            q = parts[1].strip()
            try:
                bid = await api_client.find_bodega(q)
                if bid is None:
                    console.print(f"[yellow]Bodega '{q}' no encontrada. 'inventario' muestra el catálogo.[/yellow]")
                else:
                    inv = await api_client.get_inventory(bodega_id=bid)
                    show_inventory(inv)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
            console.print()
            continue
        if low.startswith("sospechosos"):
            parts = low.split(maxsplit=1)
            prod = parts[1] if len(parts) > 1 else None
            try:
                rows = await api_client.get_sospechosos(prod)
                show_sospechosos(rows)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
            console.print()
            continue

        try:
            await handle_query(state, user_input)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
        console.print()

    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--session", help="Override session id")
    a = p.parse_args()
    if a.session:
        global SESSION_ID
        SESSION_ID = a.session
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    sys.exit(main())
