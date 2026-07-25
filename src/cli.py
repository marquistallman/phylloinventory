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
import logging
import os
import sys
import uuid
from typing import Any

import httpx

logger = logging.getLogger("cli")
from rich.align import Align
from rich import box as _box_module
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

#  Carga .env / .env.example ANTES de importar api_client / tts_client,
#  que leen os.getenv() a nivel de modulo. Prioridad: shell > .env > .env.example.
from .env_loader import load_env
load_env()

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
    narrator = cfg.get("narrator", "default")
    narrator_model = cfg.get("narrator_model", "")
    cloud = cfg.get("cloud_enabled", False)
    cloud_tag = " [bold magenta]CLOUD[/bold magenta]" if cloud else ""
    n_color = "magenta" if narrator == "llm" else "cyan"

    voice_line = (
        "[green]Voz: OK (microfono disponible)[/green]"
        if voice_ok
        else "[yellow]Voz: sounddevice no instalado (pip install sounddevice)[/yellow]"
    )
    backends_line = (
        f"[dim]LLM: [bold cyan]{llm}[/bold cyan]  "
        f"STT: [bold cyan]{stt}[/bold cyan]  "
        f"TTS: [bold cyan]{tts}[/bold cyan]  "
        f"Narrator: [{n_color}]{narrator}[/{n_color}][/dim]{cloud_tag}"
    )
    narrator_model_line = (
        f"[dim]  Narrator model: [cyan]{narrator_model}[/cyan][/dim]\n" if narrator_model else ""
    )
    panel = Panel(
        Align.center(
            Text.from_markup(
                "[bold green]Cactus Inventory - Microservicios[/bold green]\n"
                f"Session: [dim]{SESSION_ID}[/dim]\n"
                f"{backends_line}\n"
                f"{narrator_model_line}"
                f"[dim]{voice_line}[/dim]\n"
                "[dim]Kalman evaluado por worker Go - Cola en PostgreSQL[/dim]\n\n"
                "[yellow]Comandos:[/yellow]\n"
                "  texto libre              -> enviar al LLM\n"
                "  voz                      -> dictar por microfono\n"
                "  tts <texto>              -> probar TTS (sintetiza y reproduce)\n"
                "  narrate <event> k=v...   -> probar narrador (templates vs LLM)\n"
                "  cloud on|off|status      -> toggle cloud (Eleven Labs + OpenRouter)\n"
                "  voices                   -> listar voces Eleven Labs disponibles\n"
                "  models [all|select <slug>] -> listar/seleccionar modelos OpenRouter\n"
                "  inventario               -> ver catalogo (1 fila x producto)\n"
                "  inventario <bodega>      -> stock por bodega\n"
                "  sospechosos [producto]   -> auditoria Kalman\n"
                "  salir / exit / q         -> cerrar\n"
                "  ayuda / help             -> mostrar este banner\n"
                "  limpiar / clear          -> limpiar pantalla"
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
#  Narracion TTS (kokoro/elevenlabs). Fire-and-forget: nunca bloquea el loop.
#  El TEXTO se pide al gateway (/api/narrate) — el decide si usa templates
#  hardcodeados o un LLM para reformular (default: google/gemma-4-31b-it:free).
# =====================================================================

def _narrate(phrase: str) -> None:
    """Lanza la reproduccion de TTS en background. Si el servicio no esta
    disponible o sounddevice falla, simplemente no suena — el flujo sigue."""
    if not phrase:
        return
    #  create_task sin await: el TTS se reproduce en su thread.
    asyncio.create_task(tts_client.speak(phrase))


def _narrate_event(event: str, data: dict, *, timeout: float = 5.0) -> None:
    """Pide al gateway la frase natural del evento y la reproduce.

    Si el gateway no responde o falla, usa un fallback local ultra-corto
    para que el flujo nunca quede en silencio absoluto.
    """
    async def _do():
        try:
            res = await api_client.narrate(event, data, timeout=timeout)
            _narrate(res.get("text", ""))
        except Exception as e:
            logger.debug("narrate fallo, fallback local: %s", e)
            _narrate(_narrate_fallback(event, data))
    asyncio.create_task(_do())


def _narrate_fallback(event: str, data: dict) -> str:
    """Fallback ultra-corto si el gateway no responde. Suena robotico pero
    nunca se pierde el aviso al usuario."""
    producto = (data.get("producto") or "el producto").lower()
    if event == "aceptada":
        return f"Listo. {producto} actualizado."
    if event == "sospechosa":
        return f"Atencion. Movimiento sospechoso de {producto}."
    if event == "confirmada":
        return "Confirmado."
    if event == "rechazada":
        return "Rechazado."
    if event == "consulta":
        return f"{producto}: {data.get('stock_actual', '?')} {data.get('unidad', '')}."
    if event == "sospechosos":
        return f"{data.get('total', 0)} movimientos sospechosos."
    if event == "registrar_manual":
        return f"Anotado: {producto}."
    if event == "invalid":
        return f"No se pudo procesar: {data.get('args', {}).get('producto', '')}."
    return f"Evento: {event}."


def _narrate_aceptada(tool_name: str, args: dict, inv: dict | list | None) -> None:
    if isinstance(inv, dict) and "stock_actual" in inv:
        stock = inv["stock_actual"]
        bodega = inv.get("bodega") or "la bodega"
    elif isinstance(inv, list) and inv:
        stock = inv[0].get("stock_actual", "?")
        bodega = inv[0].get("bodega", "la bodega")
    else:
        stock = None
        bodega = "la bodega"
    _narrate_event("aceptada", {
        "tool_name": tool_name,
        "producto": args.get("producto"),
        "cantidad": args.get("cantidad"),
        "unidad": args.get("unidad") or "",
        "stock_actual": stock,
        "bodega": bodega,
        "tipo": "entrada" if tool_name == "agregar_inventario" else "salida",
    })


def _narrate_sospechosa(args: dict, puntaje: float, residual: float) -> None:
    _narrate_event("sospechosa", {
        "tool_name": args.get("tool_name"),
        "producto": args.get("producto"),
        "cantidad": args.get("cantidad"),
        "unidad": args.get("unidad") or "",
        "puntaje_riesgo": puntaje,
        "tipo": "entrada" if args.get("tool_name") == "agregar_inventario" else "salida",
    })


def _narrate_confirmada() -> None:
    _narrate_event("confirmada", {})


def _narrate_rechazada() -> None:
    _narrate_event("rechazada", {})


def _narrate_consulta(inv) -> None:
    if isinstance(inv, dict) and "stock_actual" in inv:
        _narrate_event("consulta", {
            "producto": inv.get("nombre"),
            "stock_actual": inv.get("stock_actual"),
            "unidad": inv.get("unidad", ""),
            "bodega": inv.get("bodega", "la bodega"),
        })
    elif isinstance(inv, list) and inv:
        if len(inv) == 1:
            r = inv[0]
            _narrate_event("consulta", {
                "producto": r.get("nombre"),
                "stock_actual": r.get("stock_actual"),
                "unidad": r.get("unidad", ""),
                "bodega": r.get("bodega", "la bodega"),
            })
        else:
            total = sum(float(r.get("stock_actual") or 0) for r in inv)
            _narrate_event("consulta", {
                "producto": "varios",
                "stock_actual": total,
                "unidad": "unidades",
                "bodega": f"{len(inv)} bodegas",
            })


def _narrate_sospechosos(rows: list[dict]) -> None:
    if not rows:
        _narrate_event("sospechosos", {"total": 0})
        return
    top = max(rows, key=lambda r: r["puntaje_riesgo"])
    _narrate_event("sospechosos", {
        "total": len(rows),
        "top_producto": top.get("producto_nombre"),
        "top_cantidad": top.get("cantidad_reportada"),
        "top_puntaje": top.get("puntaje_riesgo"),
        "top_tipo": top.get("tipo"),
        "top_unidad": "Unidad",  # no la tenemos en el row; default
    })


def _narrate_invalid(tool_name: str, args: dict) -> None:
    """Caso 'no se encolo' — el LLM devolvio la tool pero el producto/cantidad
    no son validos, asi que la CLI la descarto y debe avisar al usuario."""
    _narrate_event("invalid", {
        "tool_name": tool_name,
        "args": args,
    })


def _narrate_no_action() -> None:
    """Caso 'tool_calls vacio' — el LLM no decidio nada util."""
    _narrate_event("no_action", {})


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
            reason = resp.get("fallback_reason", "")
            #  Reason viene como "service_down:openrouter-service (no esta corriendo; arrancalo con ...)"
            #  o "http_429:rate_limited — gemma-4-31b-it:free is temporarily rate-limited..."
            if reason.startswith("service_down:"):
                #  Extraer el comando docker compose del mensaje (si esta)
                console.print(
                    f"  [yellow]i cloud=[/yellow][magenta]{requested}[/magenta]"
                    f"[yellow] fallo, use local=[cyan]{backend}[/cyan][/yellow]"
                )
                console.print(f"    [dim]motivo:[/dim] [yellow]{reason}[/yellow]")
            elif "rate_limited" in reason or "http_429" in reason:
                console.print(
                    f"  [yellow]i cloud=[/yellow][magenta]{requested}[/magenta]"
                    f"[yellow] rate-limited, fallback a local=[cyan]{backend}[/cyan][/yellow]"
                )
                console.print(f"    [dim]detalle:[/dim] [yellow]{reason}[/yellow]")
            elif reason:
                console.print(
                    f"  [yellow]i cloud=[/yellow][magenta]{requested}[/magenta]"
                    f"[yellow] fallo, fallback a local=[cyan]{backend}[/cyan][/yellow]"
                )
                console.print(f"    [dim]motivo:[/dim] [yellow]{reason}[/yellow]")
            else:
                console.print(
                    f"  [yellow]i cloud=[/yellow][magenta]{requested}[/magenta]"
                    f"[yellow] fallo, fallback a local=[cyan]{backend}[/cyan][/yellow]"
                )
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
    """`cloud on|off|status` o `cloud stt=elevenlabs tts=elevenlabs` etc.

    Comportamiento:
      - sin args / "status"      -> muestra config actual
      - "on" / "off"             -> toggle cloud global
      - "k=v k=v ..."            -> override por-backend (cualquier combinacion)
      - cualquier otra cosa      -> muestra config + hint de uso
    """
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
    #  Si NINGUN token tiene '=', asumimos que el usuario quiso "status"
    #  y mostramos la config + hint de uso (mas util que tirar "ignoro").
    tokens = args.split()
    has_kv = any("=" in t for t in tokens)
    if not has_kv:
        console.print(
            f"[yellow]Subcomando '{args}' no reconocido. Mostrando estado actual:[/yellow]"
        )
        try:
            cfg = await api_client.get_config()
        except Exception as e:
            console.print(f"[red]No se pudo leer config: {e}[/red]")
            return
        _print_config(cfg)
        console.print()
        console.print(
            "[dim]uso:[/dim]\n"
            "  [cyan]cloud on|off[/cyan]              toggle global\n"
            "  [cyan]cloud status[/cyan]              ver config actual\n"
            "  [cyan]cloud llm=openrouter[/cyan]      override por-backend (stt, tts, llm)\n"
            "  [cyan]cloud stt=whisper tts=elevenlabs[/cyan]  (cualquier combinacion)\n"
            "  [cyan]cloud llm=auto[/cyan]            volver al toggle global para un backend"
        )
        return
    overrides: dict = {}
    for token in tokens:
        if "=" not in token:
            console.print(f"[yellow]ignoro token '{token}' (esperaba k=v)[/yellow]")
            continue
        k, v = token.split("=", 1)
        k = k.strip().lower()
        v = v.strip().lower()
        if k not in ("llm", "stt", "tts", "narrator"):
            console.print(f"[yellow]backend '{k}' no soportado (usa llm/stt/tts/narrator)[/yellow]")
            continue
        overrides[k] = v
    if not overrides:
        console.print(
            "[yellow]uso: cloud on|off|status | cloud llm=needle stt=whisper tts=kokoro[/yellow]"
        )
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
    narrator = cfg.get("narrator", "default")
    narrator_model = cfg.get("narrator_model", "")
    n_color = "magenta" if narrator == "llm" else "green"
    console.print(
        f"[{color}]cloud_enabled = {cloud}[/{color}]\n"
        f"  LLM       = [cyan]{cfg.get('llm')}[/cyan]  (override: {cfg.get('llm_override') or 'auto'})\n"
        f"  STT       = [cyan]{cfg.get('stt')}[/cyan]  (override: {cfg.get('stt_override') or 'auto'})\n"
        f"  TTS       = [cyan]{cfg.get('tts')}[/cyan]  (override: {cfg.get('tts_override') or 'auto'})\n"
        f"  Narrator  = [{n_color}]{narrator}[/{n_color}]  model=[cyan]{narrator_model}[/cyan]\n"
        f"             (override: narrator={cfg.get('narrator_override') or 'auto'}, "
        f"model={cfg.get('narrator_model_override') or 'auto'})\n"
        f"  [dim]defaults (env): llm={cfg.get('defaults', {}).get('llm')}, "
        f"stt={cfg.get('defaults', {}).get('stt')}, tts={cfg.get('defaults', {}).get('tts')}, "
        f"narrator={cfg.get('defaults', {}).get('narrator')}, "
        f"model={cfg.get('defaults', {}).get('narrator_model')}[/dim]"
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


async def handle_models(state: SessionState, args: str) -> None:
    """`models` -> lista modelos disponibles. `models all` -> incluye todos de OpenRouter.
    `models select <slug>` -> cambia el modelo del narrador.
    `models select auto` -> vuelve al default.
    """
    args = args.strip()
    if args.startswith("select"):
        #  models select <slug>  o  models select auto
        rest = args[len("select"):].strip()
        if not rest or rest.lower() == "auto":
            try:
                cfg = await api_client.select_model("narrator", None)
            except Exception as e:
                console.print(f"[red]select fallo: {e}[/red]")
                return
            console.print(f"[green]narrator_model = auto (vuelve al default: {cfg.get('defaults', {}).get('narrator_model', '?')})[/green]")
            return
        slug = rest
        try:
            cfg = await api_client.select_model("narrator", slug)
        except Exception as e:
            console.print(f"[red]select fallo: {e}[/red]")
            return
        console.print(f"[green]narrator_model = {cfg.get('narrator_model')}[/green]")
        return
    show_all = args.lower() == "all"
    try:
        data = await api_client.list_models(all=show_all, category=None)
    except Exception as e:
        console.print(f"[red]No se pudo listar modelos: {e}[/red]")
        return
    models = data.get("models", [])
    current = data.get("current", {})
    if not models:
        console.print("[yellow]No hay modelos disponibles.[/yellow]")
        return
    table = Table(
        title=f"Modelos disponibles ({len(models)})" + (" — OpenRouter completo" if show_all else " — curados"),
        box=box.SIMPLE_HEAVY, border_style="cyan",
    )
    table.add_column("Slug", style="bold", no_wrap=True)
    table.add_column("Nombre")
    table.add_column("Costo/M in", justify="right")
    table.add_column("Costo/M out", justify="right")
    table.add_column("Free", justify="center")
    for m in models:
        slug = m.get("slug", "?")
        is_active = slug == current.get("narrator_model")
        name = m.get("name", "?")
        if is_active:
            name = f"{name}  [green]<-- narrador activo[/green]"
        cost_in = m.get("cost_in", 0)
        cost_out = m.get("cost_out", 0)
        is_free = m.get("free", False)
        free_marker = "[green]FREE[/green]" if is_free else f"${cost_in:.3f}"
        table.add_row(
            slug,
            name,
            f"${cost_in:.4f}" if not is_free else "$0",
            f"${cost_out:.4f}" if not is_free else "$0",
            free_marker,
        )
    console.print(table)
    console.print(
        f"\n  [dim]Actual: narrator=[cyan]{current.get('narrator_backend')}[/cyan] "
        f"model=[cyan]{current.get('narrator_model')}[/cyan][/dim]"
    )
    console.print(
        "\n  [yellow]uso:[/yellow]\n"
        "    [cyan]models[/cyan]                  lista curada\n"
        "    [cyan]models all[/cyan]              lista completa de OpenRouter (requiere OPENROUTER_API_KEY)\n"
        "    [cyan]models select <slug>[/cyan]     cambia el modelo del narrador\n"
        "    [cyan]models select auto[/cyan]      vuelve al default\n"
    )


async def handle_narrate_demo(state: SessionState, args: str) -> None:
    """`narrate <event> [k=v ...]` -> pide al gateway que reformule el evento.
    Util para probar el narrador y ver si el LLM lo deja mas natural.
    Ejemplos:
      narrate aceptada producto=papa cantidad=5 unidad=kg stock_actual=130
      narrate sospechosa producto=harina cantidad=50 unidad=kg puntaje_riesgo=4.2
    """
    args = args.strip()
    if not args:
        console.print(
            "[yellow]uso:[/yellow]\n"
            "  [cyan]narrate aceptada producto=papa cantidad=5 unidad=kg stock_actual=130[/cyan]\n"
            "  [cyan]narrate sospechosa producto=harina cantidad=50 puntaje_riesgo=4.2[/cyan]\n"
            "  [cyan]narrate confirmada[/cyan]\n"
            "  [cyan]narrate rechazada[/cyan]\n"
            "  [cyan]narrate consulta producto=papa stock_actual=50 unidad=kg[/cyan]"
        )
        return
    parts = args.split(maxsplit=1)
    event = parts[0]
    data: dict = {}
    if len(parts) > 1:
        for token in parts[1].split():
            if "=" not in token:
                console.print(f"[yellow]ignoro '{token}' (esperaba k=v)[/yellow]")
                continue
            k, v = token.split("=", 1)
            #  Intentar convertir a numero
            try:
                v_typed: Any = float(v) if "." in v else int(v)
            except ValueError:
                v_typed = v
            data[k] = v_typed
    #  Si es narrador LLM, dar margen (puede tardar)
    try:
        cfg = await api_client.get_config()
        timeout = 15.0 if cfg.get("narrator") == "llm" else 5.0
    except Exception:
        timeout = 10.0
    try:
        res = await api_client.narrate(event, data, timeout=timeout)
    except Exception as e:
        console.print(f"[red]narrate fallo: {e}[/red]")
        return
    text = res.get("text", "")
    backend = res.get("backend", "?")
    model = res.get("model", "?")
    console.print(
        f"  [dim]event:[/dim]  [cyan]{event}[/cyan]\n"
        f"  [dim]backend:[/dim] [magenta]{backend}[/magenta]  "
        f"[dim]model:[/dim] [magenta]{model}[/magenta]\n"
        f"  [dim]texto:[/dim]\n"
    )
    console.print(Panel(Text(text, style="bold"), border_style="green", box=box.ROUNDED))
    #  Reproducir
    await tts_client.speak(text)


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
        #  httpx.TimeoutException.__str__() devuelve "" en algunas versiones,
        #  asi que mostramos el tipo + un mensaje mas util que el string vacio.
        msg = str(e).strip() or f"{type(e).__name__} (sin detalle)"
        is_timeout = isinstance(e, httpx.TimeoutException)
        console.print(f"[red]No se pudo conectar al api-gateway ({api_client.GATEWAY_URL}): {msg}[/red]")
        if is_timeout:
            console.print(
                "  [yellow]El gateway tardo mas de 20s en responder.[/yellow]\n"
                "  [dim]Causas comunes:[/dim]\n"
                "  [dim]- el contenedor esta arrancando (espera 30s y reintenta)[/dim]\n"
                "  [dim]- un servicio dependiente (needle/kokoro/voice) no responde y cuelga el health[/dim]\n"
                "  [dim]- firewall o el puerto 8200 no esta mapeado al host[/dim]"
            )
        else:
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
        #  Match exacto "cloud" o "cloud <subcomando>" — evita falsos positivos
        #  con strings como "cloudx" o "cloud-something" que pasaban con startswith.
        if low == "cloud" or low.startswith("cloud "):
            await handle_cloud(state, user_input[len("cloud "):].strip() if low.startswith("cloud ") else "")
            console.print()
            continue
        if low == "voices":
            await handle_voices(state, "")
            console.print()
            continue
        if low == "models" or low.startswith("models "):
            await handle_models(state, user_input[len("models "):].strip() if low.startswith("models ") else "")
            console.print()
            continue
        if low == "narrate" or low.startswith("narrate "):
            await handle_narrate_demo(state, user_input[len("narrate "):].strip() if low.startswith("narrate ") else "")
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
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        console.print(
                            f"[red]El gateway no tiene el endpoint /api/audio/speak (404)[/red]\n"
                            f"  [yellow]Causa probable: el contenedor del api-gateway esta corriendo una version vieja.[/yellow]\n"
                            f"  [cyan]Reconstrui el stack:[/cyan]\n"
                            f"    [cyan]docker compose build api-gateway && docker compose up -d api-gateway[/cyan]\n"
                            f"  [dim]O si corres el gateway en local: reiniciá el proceso para que tome el main.py nuevo.[/dim]"
                        )
                    else:
                        console.print(f"[red]TTS fallo ({e.response.status_code}): {e}[/red]")
                        console.print("  [dim]Tip: 'cloud status' para ver que backend esta activo.[/dim]")
                    console.print()
                    continue
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
