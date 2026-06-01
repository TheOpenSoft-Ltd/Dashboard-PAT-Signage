import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

import rich_click as click
from rich.console import Console

from pat_sig.config import (
    CONFIG_DIR,
    KIOSK_SERVICE_NAME,
    SERVICE_NAME,
    get_project_dir,
)

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

# Kiosk: launch Chromium fullscreen pointing at the local display server.
# Targets Raspberry Pi OS: Bookworm defaults to Wayland (labwc/wayfire) on Pi
# 4/5, older releases use X11. The wrapper script picks the right
# WAYLAND_DISPLAY / DISPLAY at runtime so the same unit works on both.
KIOSK_TEMPLATE = """[Unit]
Description=PAT Signage Kiosk (Chromium)
After=graphical.target {backend}.service
Wants={backend}.service

[Service]
Type=simple
User={user}
Environment=XDG_RUNTIME_DIR=/run/user/{uid}
ExecStartPre=/bin/sh -c 'until curl -sf {url} >/dev/null; do sleep 2; done'
ExecStart={launcher}
Restart=always
RestartSec=5

[Install]
WantedBy=graphical.target
"""

# Runtime launcher: detect Wayland vs X11, then exec Chromium in kiosk mode.
# Installed at ~/.config/pat-sig/kiosk.sh and referenced by the systemd unit.
KIOSK_LAUNCHER = """#!/bin/sh
# PAT Signage kiosk launcher (Raspberry Pi OS: Wayland or X11).
export XDG_RUNTIME_DIR="${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"

# Prefer Wayland (Bookworm default on Pi 4/5).
if [ -z "$WAYLAND_DISPLAY" ] && [ -S "$XDG_RUNTIME_DIR/wayland-0" ]; then
  export WAYLAND_DISPLAY=wayland-0
fi
# Fall back to X11.
if [ -z "$WAYLAND_DISPLAY" ] && [ -z "$DISPLAY" ]; then
  export DISPLAY=:0
fi

if [ -n "$WAYLAND_DISPLAY" ]; then
  OZONE="--ozone-platform=wayland --enable-features=UseOzonePlatform"
else
  OZONE=""
fi

exec {chrome} {chrome_flags} $OZONE "{url}"
"""

# Chromium flags for a clean unattended kiosk.
CHROME_FLAGS = (
    "--kiosk "
    "--noerrdialogs "
    "--disable-infobars "
    "--disable-session-crashed-bubble "
    "--disable-translate "
    "--no-first-run "
    "--incognito "
    "--check-for-update-interval=31536000 "
    "--autoplay-policy=no-user-gesture-required"
)

# Chromium binary names, in preference order (Raspberry Pi OS ships
# chromium-browser).
CHROME_BINARIES = [
    "chromium-browser",
    "chromium",
    "google-chrome-stable",
    "google-chrome",
]


def _find_chrome() -> str | None:
    for name in CHROME_BINARIES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _install_unit(name: str, content: str) -> None:
    tmp = Path(f"/tmp/{name}.service")
    tmp.write_text(content)
    subprocess.run(
        ["sudo", "cp", str(tmp), f"/etc/systemd/system/{name}.service"],
        check=False,
    )


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8000, type=int, help="Bind port")
@click.option(
    "--kiosk/--no-kiosk",
    default=True,
    help="Also install the Chrome kiosk service.",
)
def install(host: str, port: int, kiosk: bool):
    """Install the systemd services for the signage display (+ Chrome kiosk)."""
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

    backend_unit = SERVICE_TEMPLATE.format(
        user=user, workdir=workdir, exec_start=exec_start
    )
    _install_unit(SERVICE_NAME, backend_unit)
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=False)
    subprocess.run(["sudo", "systemctl", "enable", SERVICE_NAME], check=False)
    console.print(f"[green]✓[/green] Installed service {SERVICE_NAME}")

    if kiosk:
        chrome = _find_chrome()
        if not chrome:
            console.print(
                "[yellow]![/yellow] Chromium not found — skipping kiosk "
                "service. On Raspberry Pi OS: sudo apt install -y "
                "chromium-browser, then re-run pat-sig install."
            )
        else:
            # Kiosk opens the display locally; use localhost regardless of bind.
            url = f"http://localhost:{port}/"
            uid = os.getuid()

            # Write the Wayland/X11-aware launcher to ~/.config/pat-sig/kiosk.sh.
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            launcher_path = CONFIG_DIR / "kiosk.sh"
            launcher_path.write_text(
                KIOSK_LAUNCHER.format(
                    chrome=chrome, chrome_flags=CHROME_FLAGS, url=url
                )
            )
            launcher_path.chmod(0o755)

            kiosk_unit = KIOSK_TEMPLATE.format(
                backend=SERVICE_NAME,
                user=user,
                uid=uid,
                launcher=str(launcher_path),
                url=url,
            )
            _install_unit(KIOSK_SERVICE_NAME, kiosk_unit)
            subprocess.run(
                ["sudo", "systemctl", "daemon-reload"], check=False
            )
            subprocess.run(
                ["sudo", "systemctl", "enable", KIOSK_SERVICE_NAME],
                check=False,
            )
            console.print(
                f"[green]✓[/green] Installed kiosk service "
                f"{KIOSK_SERVICE_NAME} (Chromium: {chrome})"
            )

    console.print("Start with: [cyan]pat-sig start[/cyan]")
