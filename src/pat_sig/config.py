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


def get_manage_py() -> Path:
    return get_project_dir() / "manage.py"
