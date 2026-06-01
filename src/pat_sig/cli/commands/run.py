import subprocess
import sys

import rich_click as click
from rich.console import Console

from pat_sig.config import get_manage_py, get_project_dir

console = Console()


def _manage(*args: str) -> int:
    """Run a Django manage.py command from the project dir."""
    return subprocess.run(
        [sys.executable, str(get_manage_py()), *args],
        cwd=str(get_project_dir()),
        check=False,
    ).returncode


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8000, type=int, help="Bind port")
@click.option("--migrate/--no-migrate", default=True, help="Run migrations first")
@click.option(
    "--no-server",
    is_flag=True,
    help="Only run migrations (create the DB), then exit. Used by the installer.",
)
@click.option(
    "--dev",
    is_flag=True,
    help="Use Django's development server (runserver) instead of gunicorn.",
)
def run(host: str, port: int, migrate: bool, no_server: bool, dev: bool):
    """Run the signage display server.

    Defaults to gunicorn (production WSGI). Because this app starts a single
    MQTT client + scheduler in apps.ready(), gunicorn MUST run with exactly one
    worker — more workers would spawn duplicate MQTT clients/schedulers and
    double-fire alerts and history. Use --dev only for local development.
    """
    if migrate:
        console.print("[cyan]Applying migrations...[/cyan]")
        _manage("migrate", "--noinput")

    if no_server:
        return

    project_dir = str(get_project_dir())

    if dev:
        console.print(f"[yellow]DEV server[/yellow] on {host}:{port}")
        # --noreload: a single process (MQTT client + scheduler start once).
        _manage("runserver", f"{host}:{port}", "--noreload")
        return

    console.print(f"[green]Starting gunicorn on {host}:{port}[/green]")
    # workers=1 is REQUIRED: the MQTT client + scheduler must be a singleton.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "gunicorn",
            "core.wsgi:application",
            "--workers",
            "1",
            "--bind",
            f"{host}:{port}",
        ],
        cwd=project_dir,
        check=False,
    )
