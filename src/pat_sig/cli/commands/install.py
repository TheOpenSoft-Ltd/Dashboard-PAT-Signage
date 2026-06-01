import getpass
import shutil
import subprocess
import sys
from pathlib import Path

import rich_click as click
from rich.console import Console

from pat_sig.config import SERVICE_NAME, get_project_dir

console = Console()

SERVICE_TEMPLATE = """[Unit]
Description=PAT Signage (DSM) Display Service
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={workdir}
ExecStart={exec_start}
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8000, type=int, help="Bind port")
def install(host: str, port: int):
    """Install the systemd service for the signage display."""
    user = getpass.getuser()
    workdir = str(get_project_dir())

    # Prefer the absolute path of the installed `pat-sig` entry point (created
    # by pipx at ~/.local/bin/pat-sig); fall back to `python -m pat_sig`.
    pat_sig_bin = shutil.which("pat-sig")
    if pat_sig_bin:
        exec_start = f"{pat_sig_bin} run --host {host} --port {port}"
    else:
        exec_start = (
            f"{sys.executable} -m pat_sig run --host {host} --port {port}"
        )

    unit = SERVICE_TEMPLATE.format(
        user=user, workdir=workdir, exec_start=exec_start
    )

    unit_path = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
    tmp = Path(f"/tmp/{SERVICE_NAME}.service")
    tmp.write_text(unit)
    subprocess.run(["sudo", "cp", str(tmp), str(unit_path)], check=False)
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=False)
    subprocess.run(
        ["sudo", "systemctl", "enable", SERVICE_NAME], check=False
    )
    console.print(f"[green]✓[/green] Installed service {SERVICE_NAME}")
    console.print("Start it with: [cyan]pat-sig start[/cyan]")
