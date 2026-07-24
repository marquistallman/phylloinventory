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

def show_banner(backend: str, voice_ok: bool = False) -> None:
    voice_line = (
        "[green]Voz: disponible (Whisper listo)[/green]"
        if voice_ok
        else "[yellow]Voz: no disponible (activa con: docker compose --profile with-voice up -d)[/yellow]"
    )
    panel = Panel(
        Align.center(
            Text.from_markup(
                "[bold green]Cactus Inventory - Microservicios[/bold green]\n"
                f"[dim]Backend LLM: [bold cyan]{backend}[/bold cyan]   "
                f"Session: [dim]{SESSION_ID}[/dim][/dim]\n"
                f"[dim]{voice_line}[/dim]\n"
                "[dim]Kalman evaluado por worker Go - Cola en PostgreSQL[/dim]\n\n"
                "[yellow]Comandos:[/yellow]\n"
                "  texto libre             -> enviar al LLM\n"
                "  voz                     -> dictar por microfono (WS)\n"
                "  inventario              -> ver stock\n"
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
        table.add_row(
            r["nombre"],
            str(r["stock_actual"]),
            f"{r['media_kalman']:.1f}",
            f"{r['varianza_kalman']:.1f}",
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

    tool_calls = resp.get("tool_calls", [])
    if not tool_calls:
        if state.last_sospechoso:
            console.print("[yellow]Tienes una alerta pendiente. Responde 'si' o 'no'.[/yellow]")
        else:
            console.print(
                "[yellow]No detecte ninguna accion. Prueba:[/yellow] "
                "'agrega 4 papas' / 'cuanto hay de tomate' / 'hay algo raro'"
            )
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
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
        elif ra["tool_name"] == "investigar_sospechosos":
            try:
                rows = await api_client.get_sospechosos(ra["arguments"].get("producto"))
                show_sospechosos(rows)
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
                return
        except Exception:
            pass
        console.print(f"  [bold green]V {name} ACEPTADO[/bold green] (pending #{pid})")
        state.last_sospechoso = None
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
        console.print("[red]Faltan sounddevice y/o websockets.[/red]\n  pip install sounddevice websockets")
        return

    #  Pre-check HTTP para dar un mensaje claro si voice-service no esta
    from .voice_client import check_voice_service, _http_base_from_ws
    base = _http_base_from_ws(api_client.VOICE_WS_URL)
    if not check_voice_service(f"{base}/health"):
        console.print(
            f"[red]voice-service no responde en {base}/health[/red]\n"
            f"  [yellow]Levanta el servicio con:[/yellow]\n"
            f"  [cyan]docker compose --profile with-voice up -d voice-service[/cyan]\n"
            f"  [dim]Tip: la primera vez tarda ~30s mientras Whisper descarga el modelo.[/dim]"
        )
        return

    console.print("[bold magenta]Grabando... presiona Enter para detener[/bold magenta]")
    stop = asyncio.Event()

    def _wait_enter():
        try:
            input()
        except EOFError:
            pass
        stop.set()

    #  Thread daemon (no executor): si muere el proceso, no bloquea la salida
    import threading
    enter_thread = threading.Thread(target=_wait_enter, daemon=True)
    enter_thread.start()

    try:
        text: str | None = await record_and_transcribe(api_client.VOICE_WS_URL, stop)
    except Exception as e:
        console.print(f"[red]Error de voz: {e}[/red]")
        console.print("  [dim]Tip: puedes seguir escribiendo texto normalmente.[/dim]")
        text = None
    finally:
        stop.set()
        #  Si el usuario nunca presiono Enter (path de error), el thread sigue
        #  bloqueado en input(). Esperarlo aqui evita que dos lectores peleen
        #  por stdin en el proximo prompt.
        if enter_thread.is_alive():
            console.print("[dim](presiona Enter para continuar)[/dim]")
            await asyncio.get_event_loop().run_in_executor(None, enter_thread.join)
        print()  # limpia el [voz parcial]

    if text is None:
        return  # el error ya se imprimio arriba
    if not text:
        console.print("[yellow]No se escucho nada util.[/yellow]")
        return

    console.print(f"  [dim]Escuchado:[/dim] [bold magenta]{text}[/bold magenta]")
    await handle_query(state, text)


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
        backend = h.get("backend", "?")
    except Exception as e:
        console.print(f"[red]No se pudo conectar al api-gateway: {e}[/red]")
        console.print("[yellow]Asegurate de que docker compose este arriba.[/yellow]")
        return 1

    #  Estado del voice-service
    from .voice_client import check_voice_service, _http_base_from_ws, available as voice_available_lib
    voice_ok = False
    if voice_available_lib():
        base = _http_base_from_ws(api_client.VOICE_WS_URL)
        voice_ok = check_voice_service(f"{base}/health")

    show_banner(backend, voice_ok=voice_ok)
    try:
        inv = await api_client.get_inventory()
        if isinstance(inv, list):
            console.print()
            show_inventory(inv)
            console.print()
    except Exception:
        pass

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
            show_banner(backend)
            continue
        if low in CLEAR:
            console.clear()
            show_banner(backend)
            continue
        if low == "voz":
            await handle_voice(state)
            console.print()
            continue
        if low == "inventario":
            try:
                inv = await api_client.get_inventory()
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
