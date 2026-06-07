from __future__ import annotations  # PEP 604 (X | None) on Python 3.9

import getpass
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import rich_click as click
from rich.console import Console

from pat_sig.config import (
    KIOSK_SERVICE_NAME,
    SERVICE_NAME,
    STREAM_SCRIPT_PATH,
    STREAM_SERVICE_NAME,
    get_env_path,
    get_project_dir,
    get_stream_script,
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
#
# IMPORTANT: the kiosk runs as the user who OWNS the seat's graphical session
# (the autologin desktop user, e.g. `pi`) — not the user running this
# installer, which may be a separate headless admin account. {display_env}
# carries the DISPLAY/XAUTHORITY (X11) or WAYLAND_DISPLAY needed to reach it.
KIOSK_TEMPLATE = """[Unit]
Description=PAT Signage Kiosk (Chromium)
After=graphical.target {backend}.service
Wants={backend}.service

[Service]
Type=simple
User={user}
Environment=XDG_RUNTIME_DIR=/run/user/{uid}
{display_env}ExecStartPre=/bin/sh -c 'until curl -sf {url} >/dev/null; do sleep 2; done'
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

# Screen -> RTMP streamer. Runs as the graphical-session user (needs the
# X11/Wayland display) and executes the bundled stream.sh. RTMP_URL and any
# STREAM_* tuning are baked in as Environment= lines at install time (read from
# the device .env) so the service works even when the graphical-session user
# differs from the installer and can't read the installer's .env.
STREAM_TEMPLATE = """[Unit]
Description=PAT Signage Screen Stream (RTMP)
After=graphical.target {kiosk}.service
Wants=graphical.target

[Service]
Type=simple
User={user}
Environment=XDG_RUNTIME_DIR=/run/user/{uid}
{display_env}{stream_env}ExecStart={script}
Restart=always
RestartSec=10

[Install]
WantedBy=graphical.target
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


# The kiosk launcher is installed to a system path (not the admin user's
# home) so the graphical-session user can execute it.
KIOSK_LAUNCHER_PATH = "/usr/local/bin/pat-sig-kiosk.sh"


def _install_unit(name: str, content: str) -> None:
    tmp = Path(f"/tmp/{name}.service")
    tmp.write_text(content)
    subprocess.run(
        ["sudo", "cp", str(tmp), f"/etc/systemd/system/{name}.service"],
        check=False,
    )


def _install_executable(dest: str, content: str) -> None:
    tmp = Path(f"/tmp/{Path(dest).name}")
    tmp.write_text(content)
    subprocess.run(["sudo", "cp", str(tmp), dest], check=False)
    subprocess.run(["sudo", "chmod", "755", dest], check=False)


def _read_env_values(keys: tuple[str, ...]) -> dict[str, str]:
    """Read selected KEY=VALUE pairs from the device .env (missing file -> {})."""
    values: dict[str, str] = {}
    try:
        text = get_env_path().read_text()
    except OSError:
        return values
    wanted = set(keys)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in wanted:
            values[key] = value.strip()
    return values


def _loginctl_show(session_id: str) -> dict:
    out = subprocess.run(
        [
            "loginctl", "show-session", session_id,
            "-p", "Name", "-p", "Type", "-p", "Active",
            "-p", "Display", "-p", "Seat", "-p", "State",
        ],
        capture_output=True, text=True, check=False,
    ).stdout
    props = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    return props


def _detect_display_session():
    """Find the local graphical session the kiosk should attach to.

    Returns (user, uid, session_type, display, xauthority) for the active
    seat's x11/wayland session, or None. The owner is typically the autologin
    desktop user (e.g. `pi`), which is NOT necessarily the user running this
    installer.
    """
    try:
        listing = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True, text=True, check=False,
        ).stdout
    except FileNotFoundError:
        return None

    best = None
    for line in listing.splitlines():
        parts = line.split()
        if not parts:
            continue
        props = _loginctl_show(parts[0])
        if props.get("Type") not in ("x11", "wayland"):
            continue
        user = props.get("Name")
        if not user:
            continue
        try:
            uid = pwd.getpwnam(user).pw_uid
        except KeyError:
            continue
        cand = (
            user,
            uid,
            props["Type"],
            props.get("Display") or ":0",
            f"/home/{user}/.Xauthority",
        )
        if props.get("Active") == "yes" and props.get("Seat"):
            return cand
        best = best or cand
    return best


def _autologin_user():
    """Fall back to the lightdm autologin user when no session is active yet."""
    try:
        text = Path("/etc/lightdm/lightdm.conf").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("autologin-user=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip() or None
    return None


def _resolve_kiosk_session(install_user: str):
    """Decide which user/uid/display the kiosk unit should target."""
    session = _detect_display_session()
    if session:
        return session
    name = _autologin_user()
    if not name:
        name = install_user
    try:
        uid = pwd.getpwnam(name).pw_uid
    except KeyError:
        uid = os.getuid()
    return (name, uid, "x11", ":0", f"/home/{name}/.Xauthority")


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8000, type=int, help="Bind port")
@click.option(
    "--kiosk/--no-kiosk",
    default=True,
    help="Also install the Chrome kiosk service.",
)
@click.option(
    "--stream/--no-stream",
    default=True,
    help="Also install the screen->RTMP stream service (needs RTMP_URL in .env).",
)
def install(host: str, port: int, kiosk: bool, stream: bool):
    """Install the systemd services for the signage display (+ kiosk + stream)."""
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

    # Both the kiosk and the streamer must run as the user that owns the
    # graphical session (autologin desktop user, not necessarily the installer).
    # Resolve it once and derive the DISPLAY/WAYLAND environment they share.
    k_user = k_uid = k_type = k_display = k_xauth = None
    display_env = ""
    if kiosk or stream:
        k_user, k_uid, k_type, k_display, k_xauth = _resolve_kiosk_session(user)
        if k_type == "wayland":
            display_env = "Environment=WAYLAND_DISPLAY=wayland-0\n"
        else:
            display_env = (
                f"Environment=DISPLAY={k_display}\n"
                f"Environment=XAUTHORITY={k_xauth}\n"
            )

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

            # Install the Wayland/X11-aware launcher to a system path the
            # graphical-session user can execute.
            _install_executable(
                KIOSK_LAUNCHER_PATH,
                KIOSK_LAUNCHER.format(
                    chrome=chrome, chrome_flags=CHROME_FLAGS, url=url
                ),
            )

            kiosk_unit = KIOSK_TEMPLATE.format(
                backend=SERVICE_NAME,
                user=k_user,
                uid=k_uid,
                display_env=display_env,
                launcher=KIOSK_LAUNCHER_PATH,
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
            target = "wayland-0" if k_type == "wayland" else k_display
            console.print(
                f"[green]✓[/green] Installed kiosk service "
                f"{KIOSK_SERVICE_NAME} as user '{k_user}' "
                f"({k_type} {target}; Chromium: {chrome})"
            )

    if stream:
        env_vals = _read_env_values(
            (
                "RTMP_URL",
                "STREAM_FPS",
                "STREAM_RESOLUTION",
                "STREAM_BITRATE",
                "STREAM_ENCODER",
                "STREAM_AUDIO",
            )
        )
        rtmp = env_vals.get("RTMP_URL")
        if not rtmp:
            console.print(
                "[yellow]![/yellow] RTMP_URL not set — skipping stream service. "
                f"Add RTMP_URL=rtmp://… to {get_env_path()} and re-run "
                "pat-sig install."
            )
        else:
            # Bake the stream config into the unit so it works even when the
            # graphical-session user can't read the installer's .env.
            stream_env = "".join(
                f"Environment={key}={val}\n"
                for key, val in env_vals.items()
                if val
            )
            # Copy the bundled stream.sh to a system path the graphical user
            # can execute (same reason as the kiosk launcher).
            _install_executable(STREAM_SCRIPT_PATH, get_stream_script().read_text())

            stream_unit = STREAM_TEMPLATE.format(
                kiosk=KIOSK_SERVICE_NAME,
                user=k_user,
                uid=k_uid,
                display_env=display_env,
                stream_env=stream_env,
                script=STREAM_SCRIPT_PATH,
            )
            _install_unit(STREAM_SERVICE_NAME, stream_unit)
            subprocess.run(
                ["sudo", "systemctl", "daemon-reload"], check=False
            )
            subprocess.run(
                ["sudo", "systemctl", "enable", STREAM_SERVICE_NAME],
                check=False,
            )
            console.print(
                f"[green]✓[/green] Installed stream service "
                f"{STREAM_SERVICE_NAME} as user '{k_user}' → {rtmp}"
            )

    console.print("Start with: [cyan]pat-sig start[/cyan]")
