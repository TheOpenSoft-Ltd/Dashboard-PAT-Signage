import rich_click as click
from rich.console import Console
from rich.panel import Panel

from pat_sig.config import get_env_path, get_project_dir

console = Console()


@click.command()
@click.option(
    "--device-id", prompt="Device ID", help="Device identifier (e.g. PAT-DXXXXX)"
)
@click.option("--dsm-id", prompt="DSM ID", help="DSM (signage) UUID")
def init(device_id: str, dsm_id: str):
    """Initialize PAT Signage configuration (.env)."""
    get_project_dir().mkdir(parents=True, exist_ok=True)
    env_path = get_env_path()

    console.print(
        Panel.fit(
            "[bold cyan]PAT Signage Initialization[/bold cyan]",
            border_style="cyan",
        )
    )

    mqtt_broker = click.prompt("MQTT Broker Host", default="localhost")
    mqtt_port = click.prompt("MQTT Broker Port", default=1883, type=int)
    mqtt_tls = click.confirm("Enable MQTT TLS?", default=False)

    lines = [
        f"DEVICE_ID={device_id}",
        f"DSM_ID={dsm_id}",
        f"MQTT_BROKER={mqtt_broker}",
        f"MQTT_PORT={mqtt_port}",
        f"MQTT_TLS_ENABLED={'true' if mqtt_tls else 'false'}",
    ]
    env_path.write_text("\n".join(lines) + "\n")
    console.print(f"[green]✓[/green] Config written to {env_path}")
    console.print("Next: [cyan]pat-sig install[/cyan] then [cyan]pat-sig start[/cyan]")
