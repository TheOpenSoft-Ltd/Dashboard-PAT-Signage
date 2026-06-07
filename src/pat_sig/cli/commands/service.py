import subprocess

import rich_click as click
from rich.console import Console

from pat_sig.config import (
    KIOSK_SERVICE_NAME,
    SERVICE_NAME,
    STREAM_SERVICE_NAME,
)

console = Console()

# All services managed together, in start order (backend, then kiosk, then
# the screen streamer). Stop/uninstall walk this list in reverse.
SERVICES = [SERVICE_NAME, KIOSK_SERVICE_NAME, STREAM_SERVICE_NAME]


def _systemctl(action: str, services: list[str]) -> None:
    for svc in services:
        subprocess.run(["sudo", "systemctl", action, svc], check=False)
        console.print(f"[cyan]{action}[/cyan] {svc}")


@click.command()
def start():
    """Start the signage + kiosk services."""
    _systemctl("start", SERVICES)


@click.command()
def stop():
    """Stop the signage + kiosk services."""
    # Stop kiosk first, then the backend.
    _systemctl("stop", list(reversed(SERVICES)))


@click.command()
def restart():
    """Restart the signage + kiosk services."""
    _systemctl("restart", SERVICES)


def _select_service(kiosk: bool, stream: bool) -> str:
    if kiosk:
        return KIOSK_SERVICE_NAME
    if stream:
        return STREAM_SERVICE_NAME
    return SERVICE_NAME


@click.command()
@click.option("--kiosk", is_flag=True, help="Show the kiosk service instead.")
@click.option("--stream", is_flag=True, help="Show the stream service instead.")
def status(kiosk: bool, stream: bool):
    """Show service status."""
    subprocess.run(["systemctl", "status", _select_service(kiosk, stream)], check=False)


@click.command()
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.option("--kiosk", is_flag=True, help="Show the kiosk service logs.")
@click.option("--stream", is_flag=True, help="Show the stream service logs.")
def logs(follow: bool, kiosk: bool, stream: bool):
    """Show service logs (journalctl)."""
    svc = _select_service(kiosk, stream)
    cmd = ["journalctl", "-u", svc, "-n", "100"]
    if follow:
        cmd.append("-f")
    subprocess.run(cmd, check=False)


@click.command()
def uninstall():
    """Stop, disable and remove the signage + kiosk services."""
    for svc in reversed(SERVICES):
        subprocess.run(["sudo", "systemctl", "stop", svc], check=False)
        subprocess.run(["sudo", "systemctl", "disable", svc], check=False)
        subprocess.run(
            ["sudo", "rm", "-f", f"/etc/systemd/system/{svc}.service"],
            check=False,
        )
        console.print(f"[green]✓[/green] Removed service {svc}")
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=False)
