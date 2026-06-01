import subprocess

import rich_click as click
from rich.console import Console

from pat_sig.config import KIOSK_SERVICE_NAME, SERVICE_NAME

console = Console()

# Both services managed together (backend first, then kiosk).
SERVICES = [SERVICE_NAME, KIOSK_SERVICE_NAME]


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


@click.command()
@click.option("--kiosk", is_flag=True, help="Show the kiosk service instead.")
def status(kiosk: bool):
    """Show service status."""
    svc = KIOSK_SERVICE_NAME if kiosk else SERVICE_NAME
    subprocess.run(["systemctl", "status", svc], check=False)


@click.command()
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.option("--kiosk", is_flag=True, help="Show the kiosk service logs.")
def logs(follow: bool, kiosk: bool):
    """Show service logs (journalctl)."""
    svc = KIOSK_SERVICE_NAME if kiosk else SERVICE_NAME
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
