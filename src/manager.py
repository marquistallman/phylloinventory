"""manager: CLI admin del servidor Cactus Inventory.

Dos modos:

1) Interactivo (default, sin args): un menu navegable con flechas donde
   vas eligiendo que hacer. Pensado para uso manual en el servidor.

2) No-interactive (con subcomandos): para scripting y CI, igual que antes.

Uso interactivo:
    python -m src.manager              # abre el menu principal

Uso por linea de comandos:
    python -m src.manager status
    python -m src.manager config show
    python -m src.manager migrate
    ...
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

# Carga .env (igual que el CLI)
from .env_loader import load_env
load_env()

from . import manager_tui as tui


# =====================================================================
# Constantes
# =====================================================================

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
COMPOSE_FILE = ROOT / "docker-compose.yml"
MIGRATIONS_DIR = ROOT / "db" / "migrations"
GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://127.0.0.1:8200")

SECRET_KEYS = __import__("re").compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|API|CRED|ACCESS)",
    __import__("re").IGNORECASE,
)


def _mask_value(key: str, value: str) -> str:
    if not value or not SECRET_KEYS.search(key):
        return value
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


# =====================================================================
# Helpers de Docker
# =====================================================================

def _run(cmd: list[str], *, check: bool = True, capture: bool = True, stream: bool = False) -> subprocess.CompletedProcess:
    """Ejecuta un comando.

    - stream=False (default): captura stdout/stderr y los devuelve en result.
    - stream=True: deja que el output vaya directo a la terminal (para que el
      usuario vea el progreso de `docker compose build` en vivo).
    """
    try:
        if stream:
            #  Sin capture: docker compose escribe directo a la terminal.
            #  No usamos timeout (queremos ver hasta que termine, no cortar).
            return subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=False,
                text=True,
                shell=(os.name == "nt"),
            )
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=capture,
            text=True,
            shell=(os.name == "nt"),
        )
        if check and result.returncode != 0:
            tui._console()[0].print(f"[red]comando fallo (rc={result.returncode}): {' '.join(cmd)}[/red]")
        return result
    except FileNotFoundError as e:
        tui._console()[0].print(f"[red]comando no encontrado: {cmd[0]}[/red]")
        raise


def _docker_compose(*args: str, profiles: list[str] | None = None, check: bool = True, stream: bool = False) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose"]
    for p in profiles or []:
        cmd.extend(["--profile", p])
    cmd.extend(list(args))
    return _run(cmd, check=check, stream=stream)


def _all_profiles() -> list[str]:
    return ["with-voice", "with-openrouter", "with-elevenlabs"]


# =====================================================================
# Acciones (las mismas del CLI, sin cambios funcionales)
# =====================================================================

def action_status() -> None:
    profiles = _all_profiles()
    r = _docker_compose("ps", "--format", "table {{.Name}}\t{{.Status}}\t{{.Ports}}", profiles=profiles, check=False)
    con, _ = tui._console()
    if con:
        con.print(f"[bold cyan]Containers:[/bold cyan]")
    print(r.stdout or "")
    if r.returncode != 0:
        print(f"[yellow]docker compose ps fallo: {r.stderr[:200]}[/yellow]")

    print()
    print(f"[bold cyan]Gateway health ({GATEWAY_URL}):[/bold cyan]")
    try:
        async def _h():
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{GATEWAY_URL}/health")
                return r.json()
        h = asyncio.run(_h())
        cfg = h.get("config", {})
        print(f"  status: {h.get('status')}")
        print(f"  cloud:  cloud={cfg.get('cloud_enabled')} llm={cfg.get('llm')} stt={cfg.get('stt')} tts={cfg.get('tts')}")
        print(f"  db:     {h.get('db')}")
        for name, info in h.get("services", {}).items():
            print(f"    {name}: {info.get('status')}")
    except Exception as e:
        print(f"  [red]gateway no responde: {e}[/red]")


def _load_env_file(path: Path) -> dict:
    from dotenv import dotenv_values
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _save_env_file(path: Path, data: dict) -> None:
    lines: list[str] = []
    seen: set = set()
    if ENV_EXAMPLE_PATH.exists():
        for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                lines.append(line)
                continue
            if "=" in line:
                k, _, _ = line.partition("=")
                k = k.strip()
                lines.append(f"{k}={data[k]}" if k in data else f"{k}=")
                seen.add(k)
            else:
                lines.append(line)
        new_keys = [k for k in data if k not in seen]
        if new_keys:
            lines.append("")
            lines.append("# --- Agregadas por manager ---")
            for k in new_keys:
                lines.append(f"{k}={data[k]}")
    else:
        for k, v in data.items():
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def action_config_show() -> None:
    src = ENV_PATH if ENV_PATH.exists() else ENV_EXAMPLE_PATH
    if not src.exists():
        print("[red]no hay .env ni .env.example[/red]")
        return
    data = _load_env_file(src)
    print(f"[bold cyan]{src.name}:[/bold cyan]")
    if not data:
        print("  (vacio)")
        return
    for k, v in data.items():
        print(f"  {k} = {_mask_value(k, v)}")


def action_config_set(pairs: list[str]) -> int:
    new_values = {}
    for p in pairs:
        if "=" not in p:
            print(f"[red]formato invalido: {p!r} (esperaba KEY=VALUE)[/red]")
            return 1
        k, _, v = p.partition("=")
        k = k.strip()
        if not k:
            print(f"[red]key vacia en {p!r}[/red]")
            return 1
        new_values[k] = v.strip()
    print(f"[yellow]Vas a setear {len(new_values)} variables:[/yellow]")
    for k, v in new_values.items():
        print(f"  {k} = {_mask_value(k, v)}")
    if not tui.confirm("Continuar?", default=False):
        print("[red]cancelado.[/red]")
        return 1
    data = _load_env_file(ENV_PATH) if ENV_PATH.exists() else {}
    data.update(new_values)
    _save_env_file(ENV_PATH, data)
    for k, v in new_values.items():
        os.environ[k] = v
    print(f"[green].env actualizado: {len(new_values)} keys[/green]")
    return 0


def action_config_unset(keys: list[str]) -> int:
    if not ENV_PATH.exists():
        print("[yellow].env no existe.[/yellow]")
        return 0
    data = _load_env_file(ENV_PATH)
    removed = [k for k in keys if k in data]
    for k in removed:
        del data[k]
        os.environ.pop(k, None)
    _save_env_file(ENV_PATH, data)
    print(f"[green]removidas: {removed}[/green]")
    return 0


def action_keys() -> None:
    data = _load_env_file(ENV_PATH) if ENV_PATH.exists() else _load_env_file(ENV_EXAMPLE_PATH)
    if not data:
        print("[red]no hay .env ni .env.example[/red]")
        return
    print("[bold cyan]API Keys / Secrets:[/bold cyan]")
    for k, v in data.items():
        if not SECRET_KEYS.search(k):
            continue
        has = bool(v) and v.strip() != ""
        tag = "OK   " if has else "VACIO"
        print(f"  {k:30s} [{tag}] {_mask_value(k, v)}")


def action_rebuild(services: list[str] | None) -> int:
    services = services or []
    if services:
        print(f"[yellow]rebuild: {services}[/yellow]")
        if not tui.confirm("Continuar?", default=False):
            print("[red]cancelado.[/red]")
            return 1
    else:
        if not tui.confirm("Rebuild de TODOS los servicios? (puede tardar minutos)", default=False):
            print("[red]cancelado.[/red]")
            return 1
    profiles = _all_profiles()
    print("[cyan]building...[/cyan]")
    #  stream=True: el progreso del build (cada step de Docker) se ve en vivo
    r = _docker_compose("build", *services, profiles=profiles, check=False, stream=True)
    if r.returncode != 0:
        print(f"[red]build fallo (rc={r.returncode})[/red]")
        return r.returncode
    print("[green]build OK[/green]")
    print("[cyan]up -d...[/cyan]")
    r = _docker_compose("up", "-d", *services, profiles=profiles, check=False, stream=True)
    if r.returncode != 0:
        print(f"[red]up fallo[/red]")
        return r.returncode
    print("[green]up OK[/green]")
    return 0


def action_restart(services: list[str] | None) -> int:
    services = services or []
    profiles = _all_profiles()
    r = _docker_compose("restart", *services, profiles=profiles, check=False, stream=True)
    print("[green]restart OK[/green]" if r.returncode == 0 else f"[red]restart fallo[/red]")
    return r.returncode


def action_up() -> int:
    r = _docker_compose("up", "-d", profiles=_all_profiles(), check=False, stream=True)
    print("[green]up OK[/green]" if r.returncode == 0 else "[red]up fallo[/red]")
    return r.returncode


def action_down() -> int:
    print("[red]ATENCION: down va a detener TODOS los containers.[/red]")
    try:
        r = input("[red]Escribi 'yes' (literal) para confirmar: [/red]").strip()
    except (EOFError, KeyboardInterrupt):
        r = ""
    if r != "yes":
        print("[red]cancelado.[/red]")
        return 1
    r = _docker_compose("down", check=False, stream=True)
    print("[green]down OK[/green]" if r.returncode == 0 else "[red]down fallo[/red]")
    return r.returncode


def action_logs(service: str | None, n: int = 100, follow: bool = False) -> int:
    if not service:
        #  Listar containers disponibles
        r = _docker_compose("ps", "--format", "{{.Name}}", profiles=_all_profiles(), check=False)
        print("[bold cyan]Containers disponibles:[/bold cyan]")
        print(r.stdout)
        return 0
    cmd = ["docker", "compose", "logs", "--tail", str(n)]
    if follow:
        cmd.append("-f")
    cmd.append(service)
    return subprocess.call(cmd, cwd=str(ROOT))


def _migration_applied(name: str) -> bool:
    import psycopg2
    dsn = os.getenv("DATABASE_URL", "postgres://cactus:cactus@127.0.0.1:5432/inventario")
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS _migrations (
                        filename VARCHAR(255) PRIMARY KEY,
                        applied_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                conn.commit()
                cur.execute("SELECT 1 FROM _migrations WHERE filename = %s", (name,))
                return cur.fetchone() is not None
    except Exception as e:
        print(f"[yellow]no se pudo chequear migraciones: {e}[/yellow]")
        return False


def _mark_migration_applied(name: str) -> None:
    import psycopg2
    dsn = os.getenv("DATABASE_URL", "postgres://cactus:cactus@127.0.0.1:5432/inventario")
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO _migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
            conn.commit()


def action_migrate() -> int:
    if not MIGRATIONS_DIR.exists():
        print("[yellow]no hay db/migrations/[/yellow]")
        return 0
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migrations:
        print("[yellow]no hay migraciones[/yellow]")
        return 0
    pending = []
    for m in migrations:
        if _migration_applied(m.name):
            print(f"  [dim]- {m.name} (ya aplicada)[/dim]")
        else:
            print(f"  [cyan]- {m.name} (pendiente)[/cyan]")
            pending.append(m)
    if not pending:
        print("[green]nada que aplicar[/green]")
        return 0
    if not tui.confirm(f"Aplicar {len(pending)} migracion(es)?", default=False):
        print("[red]cancelado.[/red]")
        return 1
    import psycopg2
    dsn = os.getenv("DATABASE_URL", "postgres://cactus:cactus@127.0.0.1:5432/inventario")
    for m in pending:
        print(f"[cyan]aplicando {m.name}...[/cyan]")
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(m.read_text(encoding="utf-8"))
                conn.commit()
            _mark_migration_applied(m.name)
            print(f"[green]OK {m.name}[/green]")
        except Exception as e:
            print(f"[red]fallo {m.name}: {e}[/red]")
            return 1
    print(f"[green]{len(pending)} migracion(es) aplicada(s)[/green]")
    return 0


def action_models_list() -> None:
    try:
        async def _h():
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{GATEWAY_URL}/api/models")
                return r.json()
        data = asyncio.run(_h())
    except Exception as e:
        print(f"[red]gateway no responde: {e}[/red]")
        return
    models = data.get("models", [])
    current = data.get("current", {})
    for m in models:
        slug = m.get("slug", "?")
        marker = "  [green]<-- activo[/green]" if slug == current.get("narrator_model") else ""
        free = "FREE" if m.get("free") else f"${m.get('cost_in', 0):.3f}/M"
        print(f"  {slug:42s}  {free:10s}  {m.get('name', '')}{marker}")


def action_models_select(slug: str) -> int:
    model = None if slug.lower() == "auto" else slug
    try:
        async def _h():
            async with httpx.AsyncClient(timeout=10) as client:
                payload = {"category": "narrator"}
                if model:
                    payload["model"] = model
                r = await client.post(f"{GATEWAY_URL}/api/models/select", json=payload)
                return r.json()
        data = asyncio.run(_h())
        print(f"[green]narrator_model = {data.get('narrator_model')}[/green]")
        return 0
    except Exception as e:
        print(f"[red]fallo: {e}[/red]")
        return 1


# =====================================================================
# Menu principal interactivo
# =====================================================================

def _menu_principal() -> int:
    """Menu raiz del manager. Devuelve 0 si salio por 'exit'."""
    while True:
        idx = tui.interactive_menu(
            "Cactus Inventory Manager  --  Menu Principal",
            [
                "1.  Status (containers + gateway health)",
                "2.  Config  (ver / editar .env)",
                "3.  Keys    (ver API keys enmascaradas)",
                "4.  Containers (rebuild / restart / up / down / logs)",
                "5.  Migrations (aplicar SQL pendientes)",
                "6.  Models  (listar / seleccionar OpenRouter)",
                "0.  Salir",
            ],
            hint="Up/Down o j/k para mover, Enter para seleccionar, Esc para salir",
        )
        if idx == -1 or idx == 6:
            print("[dim]Hasta luego.[/dim]")
            return 0
        try:
            if idx == 0:
                _submenu_status()
            elif idx == 1:
                _submenu_config()
            elif idx == 2:
                _submenu_keys()
            elif idx == 3:
                _submenu_containers()
            elif idx == 4:
                _submenu_migrations()
            elif idx == 5:
                _submenu_models()
        except KeyboardInterrupt:
            print("\n[yellow]cancelado[/yellow]")
            tui.pause()
        except Exception as e:
            print(f"[red]error: {e}[/red]")
            tui.pause()


def _submenu_status() -> None:
    tui._clear()
    action_status()
    tui.pause()


def _submenu_config() -> None:
    while True:
        idx = tui.interactive_menu(
            "Config  --  .env",
            [
                "Mostrar .env actual (secrets enmascarados)",
                "Setear KEY=VALUE",
                "Quitar KEY",
                "Volver",
            ],
        )
        if idx == -1 or idx == 3:
            return
        tui._clear()
        if idx == 0:
            action_config_show()
            tui.pause()
        elif idx == 1:
            try:
                pairs = input("Ingrese KEY=VALUE (separados por espacio): ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if pairs:
                action_config_set(shlex.split(pairs))
            tui.pause()
        elif idx == 2:
            try:
                k = input("KEY a quitar: ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if k:
                action_config_unset([k])
            tui.pause()


def _submenu_keys() -> None:
    tui._clear()
    action_keys()
    tui.pause()


def _submenu_containers() -> None:
    while True:
        idx = tui.interactive_menu(
            "Containers  --  docker compose",
            [
                "Status (ps + format)",
                "Rebuild [servicio...]",
                "Restart [servicio...]",
                "Up (arranca con profiles activos)",
                "Down (para todo -- destructivo)",
                "Logs (tail de un servicio)",
                "Volver",
            ],
        )
        if idx == -1 or idx == 6:
            return
        tui._clear()
        if idx == 0:
            action_status()
            tui.pause()
        elif idx == 1:
            try:
                s = input("Servicios a rebuild (vacio = todos): ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            action_rebuild(s.split() if s else None)
            tui.pause()
        elif idx == 2:
            try:
                s = input("Servicios a restart (vacio = todos): ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            action_restart(s.split() if s else None)
            tui.pause()
        elif idx == 3:
            action_up()
            tui.pause()
        elif idx == 4:
            action_down()
            tui.pause()
        elif idx == 5:
            try:
                s = input("Servicio (vacio = listar): ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if s:
                n_str = input("Cantidad de lineas [100]: ").strip() or "100"
                follow = tui.confirm("Stream continuo (-f)?", default=False)
                try:
                    n = int(n_str)
                except ValueError:
                    n = 100
                action_logs(s, n=n, follow=follow)
            else:
                action_logs(None)
            tui.pause()


def _submenu_migrations() -> None:
    tui._clear()
    action_migrate()
    tui.pause()


def _submenu_models() -> None:
    while True:
        idx = tui.interactive_menu(
            "Models  --  OpenRouter",
            [
                "Listar modelos disponibles (curados)",
                "Cambiar modelo del narrador",
                "Volver",
            ],
        )
        if idx == -1 or idx == 2:
            return
        tui._clear()
        if idx == 0:
            action_models_list()
            tui.pause()
        elif idx == 1:
            try:
                slug = input("Slug del modelo (o 'auto' para reset): ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if slug:
                action_models_select(slug)
            tui.pause()


# =====================================================================
# Entry point: detecta modo interactivo vs CLI
# =====================================================================

def main() -> int:
    #  Sin args -> menu interactivo
    if len(sys.argv) == 1:
        try:
            return _menu_principal()
        except KeyboardInterrupt:
            print("\n[dim]Hasta luego.[/dim]")
            return 0

    #  Con args -> modo CLI clasico (back-compat)
    parser = argparse.ArgumentParser(
        prog="manager",
        description="CLI admin de Cactus Inventory",
        add_help=True,
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status")
    p = sub.add_parser("config")
    cs = p.add_subparsers(dest="config_cmd")
    cs.add_parser("show")
    p_set = cs.add_parser("set")
    p_set.add_argument("pairs", nargs="+")
    p_unset = cs.add_parser("unset")
    p_unset.add_argument("keys", nargs="+")
    sub.add_parser("keys")
    p = sub.add_parser("rebuild")
    p.add_argument("services", nargs="*")
    p = sub.add_parser("restart")
    p.add_argument("services", nargs="*")
    sub.add_parser("up")
    sub.add_parser("down")
    p = sub.add_parser("logs")
    p.add_argument("service", nargs="?")
    p.add_argument("-n", type=int, default=100)
    p.add_argument("-f", "--follow", action="store_true")
    sub.add_parser("migrate")
    p = sub.add_parser("models")
    p.add_argument("--all", action="store_true")
    p_sel = sub.add_parser("select")
    p_sel.add_argument("slug")

    args = parser.parse_args()

    if args.cmd == "status":
        action_status()
    elif args.cmd == "config":
        if args.config_cmd == "show":
            action_config_show()
        elif args.config_cmd == "set":
            return action_config_set(args.pairs)
        elif args.config_cmd == "unset":
            return action_config_unset(args.keys)
    elif args.cmd == "keys":
        action_keys()
    elif args.cmd == "rebuild":
        return action_rebuild(args.services or None)
    elif args.cmd == "restart":
        return action_restart(args.services or None)
    elif args.cmd == "up":
        return action_up()
    elif args.cmd == "down":
        return action_down()
    elif args.cmd == "logs":
        return action_logs(args.service, args.n, args.follow)
    elif args.cmd == "migrate":
        return action_migrate()
    elif args.cmd == "models":
        if hasattr(args, "slug"):
            return action_models_select(args.slug)
        action_models_list()
    return 0


if __name__ == "__main__":
    sys.exit(main())
