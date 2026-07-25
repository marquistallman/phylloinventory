"""tui: helpers de TUI cross-platform para el manager.

Lee teclas con flechas sin necesidad de Enter (msvcrt en Windows,
termios en Unix). Render con `rich` (fallback a print).
"""
from __future__ import annotations

import os
import sys
from typing import Iterable


# =====================================================================
#  Lectura de teclas (raw mode, sin esperar Enter)
# =====================================================================

def _getch() -> str:
    """Lee una tecla. Devuelve un string. Casos especiales:
       - 'UP', 'DOWN', 'LEFT', 'RIGHT' para flechas
       - 'ENTER' para Enter/Return
       - 'ESC' para la tecla Escape
       - 'CTRL_C' para Ctrl+C (cancela el menu)
       - cualquier otro caracter literal.
    """
    if os.name == "nt":
        return _getch_windows()
    return _getch_unix()


def _getch_windows() -> str:
    import msvcrt
    ch = msvcrt.getch()
    #  msvcrt devuelve un byte. Para flechas, el primer byte es 0xE0
    #  (o 0x00) y el segundo es el codigo: H=Up, P=Down, K=Left, M=Right.
    if not ch:
        return ""
    b0 = ch[0]
    if b0 in (0x00, 0xE0):
        #  Es una tecla especial; leer el segundo byte
        try:
            b1 = msvcrt.getch()[0]
        except IndexError:
            return ""
        return {
            0x48: "UP",
            0x50: "DOWN",
            0x4B: "LEFT",
            0x4D: "RIGHT",
        }.get(b1, f"SPECIAL_{b1:02x}")
    if b0 == 0x1B:
        return "ESC"
    if b0 == 0x0D:
        return "ENTER"
    if b0 == 0x03:  # Ctrl+C
        return "CTRL_C"
    try:
        return ch.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _getch_unix() -> str:
    import termios
    import tty
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1).decode("utf-8", errors="ignore")
        if ch == "\x1b":
            #  Puede ser ESC sola o el inicio de una secuencia de flechas
            #  ESC [ A/B/C/D = flecha. Leemos 2 bytes mas con timeout corto.
            import select
            rlist, _, _ = select.select([fd], [], [], 0.05)
            if rlist:
                seq = os.read(fd, 2).decode("utf-8", errors="ignore")
                if seq == "[A":
                    return "UP"
                if seq == "[B":
                    return "DOWN"
                if seq == "[C":
                    return "RIGHT"
                if seq == "[D":
                    return "LEFT"
            return "ESC"
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == "\x03":
            return "CTRL_C"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# =====================================================================
#  Render
# =====================================================================

def _console():
    try:
        from rich.console import Console
        from rich import box as _box_module
        try:
            box = _box_module.box
        except AttributeError:
            box = _box_module
        return Console(), box
    except ImportError:
        return None, None


def _clear() -> None:
    """Limpia la pantalla. Windows: cls, Unix: clear."""
    os.system("cls" if os.name == "nt" else "clear")


# =====================================================================
#  Menu interactivo
# =====================================================================

def interactive_menu(
    title: str,
    options: list[str],
    *,
    hint: str = "Up/Down o j/k para mover, Enter para seleccionar, Esc para volver",
    initial_index: int = 0,
) -> int:
    """Muestra un menu navegable. Devuelve el index seleccionado, o -1 si cancela.

    options: lista de strings a mostrar. Tambien acepta tuplas (label, value)
             para devolver un valor custom (ej. un comando o un dict).
    """
    if not options:
        return -1
    #  Normalizar a (label, value)
    norm: list[tuple[str, object]] = []
    for o in options:
        if isinstance(o, tuple):
            label, value = o
        else:
            label, value = str(o), o
        norm.append((label, value))

    selected = max(0, min(initial_index, len(norm) - 1))
    con, _ = _console()
    if con is None:
        #  Fallback sin rich: menu numerado + input
        return _menu_plain(title, norm, hint, initial_index)

    while True:
        _clear()
        con.print(f"[bold cyan]{title}[/bold cyan]\n", highlight=False)
        for i, (label, _) in enumerate(norm):
            if i == selected:
                con.print(f"  [bold black on green] > {label} [/bold black on green]")
            else:
                con.print(f"    {label}")
        con.print(f"\n[dim]{hint}[/dim]", highlight=False)
        ch = _getch()
        if ch == "UP":
            selected = (selected - 1) % len(norm)
        elif ch == "DOWN":
            selected = (selected + 1) % len(norm)
        elif ch == "ENTER":
            return selected
        elif ch in ("ESC", "q", "Q", "CTRL_C"):
            return -1
        elif ch in ("j",):  # vim-style
            selected = (selected + 1) % len(norm)
        elif ch in ("k",):
            selected = (selected - 1) % len(norm)


def _menu_plain(title: str, norm: list, hint: str, initial_index: int) -> int:
    """Fallback sin rich: input numerico con flechas opcionales."""
    _clear()
    print(f"=== {title} ===\n")
    for i, (label, _) in enumerate(norm):
        print(f"  {i+1}. {label}")
    print(f"\n{hint}")
    while True:
        try:
            raw = input(f"\nElija opcion [1-{len(norm)}] (0=salir): ").strip()
        except (EOFError, KeyboardInterrupt):
            return -1
        if raw in ("0", "q", "Q", ""):
            return -1
        if raw.isdigit():
            n = int(raw) - 1
            if 0 <= n < len(norm):
                return n
        print(f"  opcion invalida: {raw!r}")


def get_value(norm_options: list, index: int) -> object:
    """Devuelve el value asociado a un index devuelto por interactive_menu."""
    if 0 <= index < len(norm_options):
        return norm_options[index][1]
    return None


def confirm(prompt: str, default: bool = False) -> bool:
    """Pregunta si/no con y/n. default se usa si el usuario solo presiona Enter."""
    suf = " [Y/n]" if default else " [y/N]"
    if _console()[0] is None:
        try:
            r = input(prompt + suf + ": ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if not r:
            return default
        return r in ("y", "yes", "s", "si")
    #  Sin rich.prompt para evitar otra dep; input simple
    try:
        r = input(prompt + suf + ": ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not r:
        return default
    return r in ("y", "yes", "s", "si")


def pause(msg: str = "Presione Enter para continuar...") -> None:
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass
