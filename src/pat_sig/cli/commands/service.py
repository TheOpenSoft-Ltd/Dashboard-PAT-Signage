import subprocess

import rich_click as click
from rich.console import Console

from pat_sig.config import SERVICE_NAME

console = Console()


def _systemctl(action: str) -> None:
    subprocess.run(["sudo", "systemctl", action, SERVICE_NAME], check=False)
    console.print(f"[cyan]{action}[/cyan] {SERVICE_NAME}")


@click.command()
def start():
    """Start the signage service."""
    _systemctl("start")


@click.command()
def stop():
    """Stop the signage service."""
    _systemctl("stop")


@click.command()
def restart():
    """Restart the signage service."""
    _systemctl("restart")


@click.command()
def status():
    """Show the signage service status."""
    subprocess.run(["systemctl", "status", SERVICE_NAME], check=False)


@click.command()
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
def logs(follow: bool):
    """Show the signage service logs (journalctl)."""
    cmd = ["journalctl", "-u", SERVICE_NAME, "-n", "100"]
    if follow:
        cmd.append("-f")
    subprocess.run(cmd, check=False)


@click.command()
def uninstall():
    """Stop, disable and remove the signage service."""
    subprocess.run(["sudo", "systemctl", "stop", SERVICE_NAME], check=False)
    subprocess.run(["sudo", "systemctl", "disable", SERVICE_NAME], check=False)
    subprocess.run(
        ["sudo", "rm", "-f", f"/etc/systemd/system/{SERVICE_NAME}.service"],
        check=False,
    )
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=False)
    console.print(f"[green]✓[/green] Removed service {SERVICE_NAME}")
