import os
import sys
from pathlib import Path

# Installed (production) location: the Django project is copied here by
# scripts/install.sh so it lives outside the pipx-managed package and keeps a
# stable .env + db.sqlite3 across upgrades.
CONFIG_DIR = Path.home() / ".config" / "pat-sig"
INSTALLED_PROJECT_DIR = CONFIG_DIR / "dsm"

# systemd service name for the signage display process (gunicorn backend).
SERVICE_NAME = "pat-sig"

# systemd service name for the kiosk (Chrome fullscreen) process.
KIOSK_SERVICE_NAME = "pat-sig-kiosk"

# systemd service name for the screen -> RTMP streamer.
STREAM_SERVICE_NAME = "pat-sig-stream"

# System path the stream script is copied to at install time, so the
# graphical-session user (which may differ from the installer) can execute it.
STREAM_SCRIPT_PATH = "/usr/local/bin/pat-sig-stream.sh"


def get_package_dir() -> Path:
    """Return the installed pat_sig package directory."""
    return Path(__file__).resolve().parent


def get_project_dir() -> Path:
    """Return the Django project dir (where manage.py + .env live).

    Prefer the installed copy under ~/.config/pat-sig/dsm; fall back to the
    in-repo source tree for local development.
    """
    if (INSTALLED_PROJECT_DIR / "manage.py").exists():
        return INSTALLED_PROJECT_DIR
    return get_package_dir() / "module" / "dsm"


def get_env_path() -> Path:
    """Return the path to the device .env consumed by Django settings."""
    return get_project_dir() / ".env"


def get_worker_dir() -> Path:
    """Return the worker scripts dir bundled in the package (stream.sh etc.)."""
    return get_package_dir() / "module" / "worker"


def get_stream_script() -> Path:
    """Return the bundled stream.sh source path inside the package."""
    return get_worker_dir() / "stream.sh"


def get_manage_py() -> Path:
    return get_project_dir() / "manage.py"


def setup_django() -> None:
    """Bootstrap Django in-process so the CLI can query the ORM directly.

    Adds the Django project dir to sys.path, points DJANGO_SETTINGS_MODULE at
    core.settings and calls django.setup(). Idempotent — safe to call more than
    once. Used by read-only commands (e.g. ``list``) that render data with rich
    rather than shelling out to manage.py.
    """
    import django

    project_dir = str(get_project_dir())
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
    # Don't start the MQTT client / scheduler for a one-shot read (see HomeConfig.ready).
    os.environ.setdefault("PAT_SIG_NO_SERVICES", "1")
    django.setup()
