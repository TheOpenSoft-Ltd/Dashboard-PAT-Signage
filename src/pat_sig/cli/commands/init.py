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

    # TLS material (device file paths). Only asked when TLS is enabled; blank
    # CA => system trust store, blank cert/key => no client (mutual-TLS) auth.
    mqtt_ca_certs = mqtt_certfile = mqtt_keyfile = ""
    if mqtt_tls:
        mqtt_ca_certs = click.prompt(
            "MQTT TLS CA cert path (optional)", default="", show_default=False
        ).strip()
        mqtt_certfile = click.prompt(
            "MQTT TLS client cert path (optional)", default="", show_default=False
        ).strip()
        mqtt_keyfile = click.prompt(
            "MQTT TLS client key path (optional)", default="", show_default=False
        ).strip()

    # Optional: RTMP target for screen streaming (pat-sig-stream service). Left
    # blank => the stream service is skipped at install time.
    rtmp_url = click.prompt(
        "RTMP stream URL (optional, blank to disable)",
        default="",
        show_default=False,
    ).strip()

    lines = [
        f"DEVICE_ID={device_id}",
        f"DSM_ID={dsm_id}",
        f"MQTT_BROKER={mqtt_broker}",
        f"MQTT_PORT={mqtt_port}",
        f"MQTT_TLS_ENABLED={'true' if mqtt_tls else 'false'}",
        f"MQTT_TLS_CA_CERTS={mqtt_ca_certs}",
        f"MQTT_TLS_CERTFILE={mqtt_certfile}",
        f"MQTT_TLS_KEYFILE={mqtt_keyfile}",
        f"RTMP_URL={rtmp_url}",
    ]
    env_path.write_text("\n".join(lines) + "\n")
    console.print(f"[green]✓[/green] Config written to {env_path}")
    console.print("Next: [cyan]pat-sig install[/cyan] then [cyan]pat-sig start[/cyan]")
