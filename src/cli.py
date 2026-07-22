import json
import os
import sys
import time
import subprocess
import signal
import atexit
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich.status import Status
from rich import box

from .agent import NeedleHTTPAgent, execute_tools, call_tool, parse_intent_fallback
from .voice import WhisperListener
from . import db_client as db

console = Console()
_tool_server_proc: Optional[subprocess.Popen] = None


def _start_tool_server() -> bool:
    global _tool_server_proc
    try:
        import requests
        requests.get("http://127.0.0.1:8000/health", timeout=2)
        console.print("[dim]Tool server ya estaba corriendo en :8000[/dim]")
        return True
    except Exception:
        pass

    console.print("[dim]Iniciando tool server en :8000...[/dim]")
    _tool_server_proc = subprocess.Popen(
        [sys.executable, "-m", "src.tool_server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        time.sleep(0.3)
        try:
            import requests
            requests.get("http://127.0.0.1:8000/health", timeout=1)
            console.print("[dim]Tool server listo.[/dim]")
            return True
        except Exception:
            pass
    console.print("[red]No se pudo iniciar el tool server[/red]")
    return False


def _stop_tool_server():
    global _tool_server_proc
    if _tool_server_proc:
        _tool_server_proc.terminate()
        _tool_server_proc = None


atexit.register(_stop_tool_server)


class InventoryDemo:
    def __init__(self, use_fallback: bool = False):
        self.use_fallback = use_fallback
        self.agent: Optional[NeedleHTTPAgent] = None
        self.voice = WhisperListener()
        self.tools_path = os.path.join(os.path.dirname(__file__), "tools.json")
        self.last_sospechoso: Optional[dict] = None

        if not use_fallback:
            self._init_needle()

    def _init_needle(self):
        self.agent = NeedleHTTPAgent()
        self.agent.load_tools(self.tools_path)

        console.print("[dim]Conectando con Needle en Docker...[/dim]")
        for attempt in range(10):
            if self.agent.health():
                console.print("[bold green]Needle conectado. (26M params, on-device tool-calling)[/bold green]")
                return
            time.sleep(3)

        console.print("[red]No se pudo conectar con Needle en Docker.[/red]")
        console.print("[yellow]Ejecuta 'docker-compose up -d' y espera a que Needle descargue el modelo.[/yellow]")
        console.print("[yellow]O usa 'python -m src.cli --fallback' para el parser de emergencia.[/yellow]")
        sys.exit(1)

    def run(self):
        if not _start_tool_server():
            sys.exit(1)
        console.clear()
        self._show_banner()
        self._check_db()
        self._loop()

    def _show_banner(self):
        agent_type = "[bold cyan]Needle (26M params)[/bold cyan]" if not self.use_fallback else "[bold yellow]FALLBACK — Parser Regex[/bold yellow]"
        banner = Panel(
            Align.center(
                Text.from_markup(
                    "[bold green]🌵 Cactus Inventory Demo[/bold green]\n"
                    f"[dim]Agente: {agent_type}[/dim]\n"
                    "[dim]Manejo de inventarios con Filtro de Kalman[/dim]\n\n"
                    "[yellow]Habla con Needle en lenguaje natural:[/yellow]\n"
                    "  'agrega 4 papas'           → añadir stock\n"
                    "  'saca 3 cebollas'          → remover stock\n"
                    "  'cuanto hay de tomate?'    → consultar\n"
                    "  'hay algo raro, investiga' → auditar sospechosos\n"
                    "  'si / dale / confirma'     → confirmar alerta\n"
                    "  'no / rechaza'             → rechazar alerta\n"
                    "  voz / hablar               → dictar por microfono 🎤\n"
                    "  salir / exit               → cerrar\n"
                ),
                vertical="middle",
            ),
            border_style="green",
            box=box.ROUNDED,
        )
        console.print(banner)

    def _check_db(self):
        try:
            rows = db.query(
                "SELECT nombre, stock_actual, media_kalman, varianza_kalman FROM productos ORDER BY nombre"
            )
            table = Table(
                title="📦 Inventario Actual",
                box=box.SIMPLE_HEAVY,
                border_style="cyan",
            )
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
            console.print()
        except Exception as e:
            console.print(f"[red]Error conectando a la BD: {e}[/red]")
            console.print("[yellow]Asegurate de que PostgreSQL este corriendo (docker-compose up -d)[/yellow]")
            sys.exit(1)

    def _loop(self):
        print()
        while True:
            prompt = "[bold yellow]⚠️  ¿Confirmas? (si/no) > [/bold yellow]" if self.last_sospechoso else "[bold cyan]📝 > [/bold cyan]"
            try:
                user_input = console.input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Cerrando demo...[/dim]")
                break

            if not user_input:
                continue

            low = user_input.lower()

            if low in ("salir", "exit", "quit", "q"):
                if self.last_sospechoso:
                    console.print("[dim]Alerta pendiente descartada. ¡Hasta luego![/dim]")
                else:
                    console.print("[dim]¡Hasta luego![/dim]")
                break

            if low in ("ayuda", "help", "?"):
                self._show_banner()
                continue

            if low in ("limpiar", "clear"):
                console.clear()
                self._show_banner()
                self._check_db()
                continue

            if low in ("voz", "hablar", "v", "mic", "microfono"):
                self._handle_voice()
                console.print()
                continue

            try:
                if self.use_fallback:
                    self._handle_fallback(user_input)
                else:
                    self._handle_needle(user_input)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

            console.print()

    def _contextualize(self, text: str) -> str:
        if self.last_sospechoso:
            s = self.last_sospechoso
            return (
                f"Hay una alerta de inventario pendiente (movimiento ID {s['movimiento_id']}, "
                f"producto {s['producto']}, {s['cantidad']} unidades, tipo {s['tipo']}). "
                f"El usuario dice: {text}"
            )
        return text

    def _handle_needle(self, text: str):
        with Status("[dim]Needle esta pensando...[/dim]", console=console) as status:
            tool_calls, raw_output = self.agent.infer(text, pending_alert=self.last_sospechoso)
            status.stop()

        if not tool_calls:
            console.print(f"  [dim]Needle raw: {raw_output[:120]}[/dim]")
            if self.last_sospechoso:
                # NO limpiar last_sospechoso: la alerta sigue viva en la BD
                console.print(
                    "[yellow]Needle no detecto una accion. Tienes una alerta pendiente:[/yellow]"
                )
                self._show_pending_alert()
                console.print("[dim]Di 'confirma el movimiento' o 'rechazalo'.[/dim]")
            else:
                console.print(
                    "[yellow]Needle no detecto ninguna accion. Prueba con:[/yellow]\n"
                    "  [cyan]'agrega 4 papas'[/cyan]   [cyan]'saca 3 cebollas'[/cyan]   [cyan]'cuanto hay de tomate'[/cyan]"
                )
            return

        if raw_output:
            console.print(f"  [dim]Needle: {raw_output[:120]}[/dim]")

        try:
            results = execute_tools(tool_calls, print_fn=console.print)
        except Exception as e:
            console.print(f"[red]Error ejecutando tool: {e}[/red]")
            return

        for result in results:
            self._display_result(result)

    def _handle_voice(self):
        if not self.voice.available():
            console.print(
                "[red]Faltan las dependencias de voz.[/red]\n"
                "  [cyan]pip install faster-whisper sounddevice[/cyan]"
            )
            return

        try:
            with Status("[dim]Cargando Whisper (local, español)...[/dim]", console=console):
                self.voice.load()
            console.print("[bold magenta]🎤 Grabando... presiona Enter para detener[/bold magenta]")
            text = self.voice.listen()
        except Exception as e:
            console.print(f"[red]Error de microfono/Whisper: {e}[/red]")
            return

        if not text:
            console.print("[yellow]No se escucho nada util. Intenta de nuevo.[/yellow]")
            return

        console.print(f"  [dim]Escuchado:[/dim] [bold magenta]{text}[/bold magenta]")
        try:
            if self.use_fallback:
                self._handle_fallback(text)
            else:
                self._handle_needle(text)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    def _handle_fallback(self, text: str):
        calls = parse_intent_fallback(text)
        if not calls:
            console.print("[yellow]No entendi el comando. Prueba: 'agrega 4 papas', 'consulta inventario', 'investiga sospechosos'[/yellow]")
            return

        results = execute_tools(calls, print_fn=console.print)

        for result in results:
            self._display_result(result)

    def _show_pending_alert(self):
        s = self.last_sospechoso
        console.print(
            f"  [yellow]  ID #{s['movimiento_id']} | {s['producto']} {s['tipo']} {s['cantidad']} | "
            f"residual: {s['residual']:.1f}s[/yellow]"
        )

    def _display_result(self, result: dict):
        success = result.get("success", False)
        message = result.get("message", "")
        data = result.get("data") or {}

        if success:
            console.print(f"  [bold green]V {message}[/bold green]")
            self.last_sospechoso = None
            return

        if "SOSPECHOSO" in message:
            mid = data.get("movimiento_id", "?")
            producto = data.get("producto", "?")
            cantidad = data.get("cantidad_reportada", "?")
            residual = data.get("residual", 0)
            riesgo = data.get("puntaje_riesgo", 0)
            riesgo_label = self._risk_label(riesgo)

            self.last_sospechoso = {
                "movimiento_id": mid,
                "producto": producto,
                "tipo": "entrada",
                "cantidad": cantidad,
                "residual": residual,
                "puntaje_riesgo": riesgo,
            }

            panel = Panel(
                Text.from_markup(
                    f"[bold yellow]ALERTA — Filtro de Kalman[/bold yellow]\n\n"
                    f"  Producto:    [cyan]{producto}[/cyan]\n"
                    f"  Cantidad:    [cyan]{cantidad}[/cyan]\n"
                    f"  ID movimiento: [cyan]{mid}[/cyan]\n"
                    f"  Residual:    [yellow]{residual:.1f}s[/yellow]\n"
                    f"  Riesgo:      [{self._risk_color(riesgo_label)}]{riesgo_label} ({riesgo:.1f}s)[/{self._risk_color(riesgo_label)}]\n\n"
                    f"[dim]El valor reportado se desvia mucho del patron esperado.[/dim]\n"
                    f"[bold]Responde naturalmente:[/bold]\n"
                    f"  [green]'si, confirma' / 'dale'[/green]   → confirmar\n"
                    f"  [red]'no, rechaza' / 'cancela'[/red]      → rechazar"
                ),
                border_style="yellow",
                box=box.ROUNDED,
            )
            console.print(panel)
            return

        if "sospechos" in message and isinstance(data, dict):
            sospechosos = data.get("sospechosos", [])
            mayor = data.get("mayor_sospechoso", {})

            if not sospechosos:
                console.print("  [green]No hay movimientos sospechosos registrados.[/green]")
                return

            table = Table(
                title="Auditoria Kalman — Sospechosos",
                box=box.SIMPLE_HEAVY,
                border_style="red",
            )
            table.add_column("#", justify="right")
            table.add_column("Producto")
            table.add_column("Tipo")
            table.add_column("Cant.", justify="right")
            table.add_column("Riesgo (s)", justify="right")
            table.add_column("Nivel")
            table.add_column("Fecha")

            for s in sospechosos:
                nivel = self._risk_label(s["puntaje_riesgo"])
                color = self._risk_color(nivel)

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

            if mayor:
                console.print(
                    f"  [bold red]MAYOR SOSPECHOSO:[/bold red] "
                    f"#{mayor['movimiento_id']} | {mayor['producto_nombre']} "
                    f"{mayor['tipo']} {mayor['cantidad_reportada']} | "
                    f"{mayor['puntaje_riesgo']:.1f}s"
                )
            return

        if "confirmado" in message.lower():
            console.print(f"  [bold green]V {message}[/bold green]")
            self.last_sospechoso = None
            return

        if "rechazado" in message.lower():
            console.print(f"  [bold red]X {message}[/bold red]")
            self.last_sospechoso = None
            return

        console.print(f"  [yellow]i {message}[/yellow]")

    @staticmethod
    def _risk_label(sigma: float) -> str:
        if sigma > 30:
            return "CRITICO"
        elif sigma > 10:
            return "ALTO"
        elif sigma > 3:
            return "MEDIO"
        return "BAJO"

    @staticmethod
    def _risk_color(label: str) -> str:
        return {"CRITICO": "red", "ALTO": "yellow", "MEDIO": "dim cyan", "BAJO": "green"}.get(label, "white")


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--fallback", action="store_true", help="Usar parser regex de emergencia en vez de Needle")
    args = parser.parse_args()

    demo = InventoryDemo(use_fallback=args.fallback)
    demo.run()


if __name__ == "__main__":
    main()
