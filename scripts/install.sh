#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="TheOpenSoft-Ltd"
REPO_NAME="Dashboard-PAT-Signage"

INSTALL_VERSION="${VERSION:-${INSTALL_VERSION:-latest}}"
AUTO_INSTALL="${AUTO_INSTALL:-true}"

OS_RELEASE_FILE="${OS_RELEASE_FILE:-/etc/os-release}"

CONFIG_DIR="$HOME/.config/pat-sig"
PROJECT_DEST="$CONFIG_DIR/dsm"
ENV_FILE="$PROJECT_DEST/.env"

usage() {
  cat <<USAGE
Usage: install.sh [OPTIONS]

Install PAT Signage (DSM) from GitHub releases.

Options:
  --version VER          Version to install
  --no-auto-install      Skip dependency installation
  -h, --help             Show help

Examples:
  curl -fsSL https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/scripts/install.sh | bash

  curl -fsSL https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/scripts/install.sh | bash -s -- --version v1.0.0

USAGE
}

log() {
  printf '[pat-sig] %s\n' "$*" >&2
}

error() {
  printf '[pat-sig][error] %s\n' "$*" >&2
  exit 1
}

detect_linux_distro() {
  [[ -f "$OS_RELEASE_FILE" ]] || error "Cannot detect Linux distribution"
  . "$OS_RELEASE_FILE"
  DISTRO_ID="$ID"
  DISTRO_VERSION="${VERSION_ID:-}"
}

# --- Python / tool provisioning -------------------------------------------
# Django 5.2 requires Python >= 3.10; this project standardizes on 3.11.
# Debian bullseye (and other older LTS) ship only Python 3.9 and have no
# `pipx` apt package, so we provision a prebuilt CPython 3.11 + pipx via uv
# WITHOUT touching the system Python.
PY_REQ_MINOR=11
UV_BIN="$HOME/.local/bin/uv"
PYTHON_BIN=""
PIPX_BIN=""

find_python() {
  # Print the path to a Python >= 3.$PY_REQ_MINOR on success; else return 1.
  local cand
  for cand in python3.13 python3.12 python3.11; do
    command -v "$cand" >/dev/null 2>&1 && { command -v "$cand"; return 0; }
  done
  if command -v python3 >/dev/null 2>&1 \
     && python3 -c "import sys; exit(0 if sys.version_info[:2] >= (3, $PY_REQ_MINOR) else 1)" 2>/dev/null; then
    command -v python3
    return 0
  fi
  return 1
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then UV_BIN="$(command -v uv)"; return; fi
  [[ -x "$UV_BIN" ]] && return
  log "Installing uv (prebuilt Python provisioner)..."
  local target tmp
  case "$(uname -m)" in
    x86_64|amd64)  target="x86_64-unknown-linux-gnu" ;;
    aarch64|arm64) target="aarch64-unknown-linux-gnu" ;;
    armv7l)        target="armv7-unknown-linux-gnueabihf" ;;
    *) error "Unsupported architecture for uv: $(uname -m)" ;;
  esac
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/uv.tar.gz" \
    "https://github.com/astral-sh/uv/releases/latest/download/uv-${target}.tar.gz" \
    || error "Failed to download uv"
  tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
  mkdir -p "$HOME/.local/bin"
  install -m 755 "$tmp/uv-${target}/uv"  "$UV_BIN"
  install -m 755 "$tmp/uv-${target}/uvx" "$HOME/.local/bin/uvx" 2>/dev/null || true
  rm -rf "$tmp"
}

ensure_python() {
  if PYTHON_BIN="$(find_python)"; then
    log "Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
    return
  fi
  log "Python >= 3.$PY_REQ_MINOR not found; provisioning prebuilt CPython 3.$PY_REQ_MINOR via uv..."
  ensure_uv
  "$UV_BIN" python install "3.$PY_REQ_MINOR" || error "Failed to install Python 3.$PY_REQ_MINOR via uv"
  PYTHON_BIN="$("$UV_BIN" python find "3.$PY_REQ_MINOR")"
  [[ -x "$PYTHON_BIN" ]] || error "Provisioned Python interpreter not found"
  ln -sf "$PYTHON_BIN" "$HOME/.local/bin/python3.$PY_REQ_MINOR"
  log "Provisioned Python: $PYTHON_BIN"
}

ensure_pipx() {
  if command -v pipx >/dev/null 2>&1; then PIPX_BIN="$(command -v pipx)"; return; fi
  if [[ -x "$HOME/.local/bin/pipx" ]]; then PIPX_BIN="$HOME/.local/bin/pipx"; return; fi
  log "Installing pipx (via $PYTHON_BIN)..."
  "$PYTHON_BIN" -m pip install --user --quiet pipx >&2 || error "Failed to install pipx"
  PIPX_BIN="$HOME/.local/bin/pipx"
  [[ -x "$PIPX_BIN" ]] || error "pipx not found after install"
  "$PIPX_BIN" ensurepath >/dev/null 2>&1 || true
}

install_system_packages() {
  # Best-effort: system tools + the kiosk browser. Python and pipx are
  # provisioned separately (ensure_python/ensure_pipx) so a missing distro
  # `pipx` package (e.g. on Debian bullseye) does not abort the install.
  detect_linux_distro
  log "Detected Linux ($DISTRO_ID)"
  local UPDATE_CMD INSTALL_CMD
  local -a PACKAGES
  case "$DISTRO_ID" in
    ubuntu|debian|raspbian|pop|linuxmint|elementary)
      UPDATE_CMD="sudo apt update"
      INSTALL_CMD="sudo apt install -y"
      # chromium-browser for the kiosk display (Raspberry Pi OS).
      PACKAGES=(git curl tar python3-pip chromium-browser)
      ;;
    fedora|rhel|centos|rocky|almalinux)
      UPDATE_CMD="sudo dnf check-update || true"
      INSTALL_CMD="sudo dnf install -y"
      PACKAGES=(git curl tar python3-pip chromium)
      ;;
    arch|archarm|manjaro|endeavouros)
      UPDATE_CMD="sudo pacman -Sy"
      INSTALL_CMD="sudo pacman -S --noconfirm"
      PACKAGES=(git curl tar python-pip chromium)
      ;;
    alpine)
      UPDATE_CMD="sudo apk update"
      INSTALL_CMD="sudo apk add"
      PACKAGES=(git curl tar py3-pip chromium)
      ;;
    *)
      log "Unknown distribution '$DISTRO_ID' — skipping system package install"
      return 0
      ;;
  esac
  log "Installing system packages: ${PACKAGES[*]}"
  eval "$UPDATE_CMD" || log "warning: package index update failed (continuing)"
  $INSTALL_CMD "${PACKAGES[@]}" || log "warning: some system packages failed to install (continuing)"
}

check_dependencies() {
  if [[ "$AUTO_INSTALL" == "true" ]]; then
    install_system_packages
    ensure_python
    ensure_pipx
  else
    PYTHON_BIN="$(find_python)" || error "Python >= 3.$PY_REQ_MINOR is required (not found)"
    command -v pipx >/dev/null 2>&1 || error "pipx is required (omit --no-auto-install to provision it)"
    PIPX_BIN="$(command -v pipx)"
    command -v git >/dev/null 2>&1 || error "git is required"
    command -v tar >/dev/null 2>&1 || error "tar is required"
  fi
  log "Dependencies ready (python: $PYTHON_BIN, pipx: $PIPX_BIN)"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version)
        [[ $# -lt 2 ]] && error "--version requires argument"
        INSTALL_VERSION="$2"
        shift 2
        ;;
      --no-auto-install)
        AUTO_INSTALL="false"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        error "Unknown option: $1"
        ;;
    esac
  done
}

get_latest_release() {
  local api_url="https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/latest"
  curl -fsSL "$api_url" \
    | sed -En 's/.*"tag_name": "([^"]+)".*/\1/p'
}

download_and_install() {
  local version="$1"
  if [[ "$version" == "latest" ]]; then
    log "Fetching latest release..."
    version=$(get_latest_release)
    log "Latest version: $version"
  fi

  local version_tag="$version"
  [[ "$version_tag" =~ ^v ]] || version_tag="v$version_tag"
  # Strip the leading "v" (and a stray leading "." from malformed tags such as
  # "v.0.1.0") to derive the asset's version number.
  local version_number="${version_tag#v}"
  version_number="${version_number#.}"
  local url="https://github.com/$REPO_OWNER/$REPO_NAME/releases/download/$version_tag/pat_sig-${version_number}.tar.gz"
  TEMP_DIR=$(mktemp -d)

  local archive="$TEMP_DIR/pat_sig.tar.gz"

  log "Downloading release..."
  curl -fsSL -o "$archive" "$url" || error "Download failed"

  # remove old version
  if "$PIPX_BIN" list 2>/dev/null | grep -q "pat-sig"; then
    log "Removing old PAT Signage version..."
    "$PIPX_BIN" uninstall pat-sig >/dev/null 2>&1 || true
  fi

  log "Installing PAT Signage (Python $("$PYTHON_BIN" --version 2>&1 | awk '{print $2}'))..."
  "$PIPX_BIN" install --python "$PYTHON_BIN" "$archive" >&2 || error "pipx install failed"
  echo "$archive"
}

copy_project() {
  local archive="$1"
  log "Extracting Django project..."
  local extract_dir
  extract_dir=$(mktemp -d)
  tar -xzf "$archive" -C "$extract_dir"

  local source_dir
  source_dir=$(find "$extract_dir" \
    -maxdepth 1 \
    -type d \
    -name "pat_sig-*")

  local project_src="$source_dir/src/pat_sig/module/dsm"
  [[ -d "$project_src" ]] || error "Project source not found in archive"

  mkdir -p "$PROJECT_DEST"
  # Preserve an existing .env + db.sqlite3 across upgrades: the release archive
  # ships template copies of both, so stash any existing ones, copy the new
  # project over, then restore them.
  local preserve_bak item
  preserve_bak=$(mktemp -d)
  for item in .env db.sqlite3; do
    [[ -f "$PROJECT_DEST/$item" ]] && cp -p "$PROJECT_DEST/$item" "$preserve_bak/$item"
  done
  cp -r "$project_src"/. "$PROJECT_DEST/"
  for item in .env db.sqlite3; do
    [[ -f "$preserve_bak/$item" ]] && cp -p "$preserve_bak/$item" "$PROJECT_DEST/$item"
  done
  rm -rf "$preserve_bak"
  log "Project copied to:"
  log "  $PROJECT_DEST"
  rm -rf "$extract_dir"
}

create_env() {
  if [[ -f "$ENV_FILE" ]]; then
    log "Config already exists: $ENV_FILE (skipping)"
    return
  fi
  log "Creating configuration..."

  local device_id dsm_id mqtt_broker mqtt_port mqtt_tls
  read -rp "Device ID (e.g. PAT-DXXXXX): " device_id
  read -rp "DSM ID (UUID): " dsm_id
  read -rp "MQTT Broker [localhost]: " mqtt_broker
  mqtt_broker="${mqtt_broker:-localhost}"
  read -rp "MQTT Port [1883]: " mqtt_port
  mqtt_port="${mqtt_port:-1883}"
  read -rp "Enable MQTT TLS? [y/N]: " mqtt_tls
  if [[ "$mqtt_tls" =~ ^[Yy]$ ]]; then mqtt_tls="true"; else mqtt_tls="false"; fi

  cat > "$ENV_FILE" <<ENV
DEVICE_ID=$device_id
DSM_ID=$dsm_id
MQTT_BROKER=$mqtt_broker
MQTT_PORT=$mqtt_port
MQTT_TLS_ENABLED=$mqtt_tls
ENV
  log "Config written to $ENV_FILE"
}

main() {
  parse_args "$@"
  # pipx and the provisioned Python live in ~/.local/bin; make sure it is on
  # PATH for this run (a non-login shell may not include it).
  export PATH="$HOME/.local/bin:$PATH"
  trap '[[ -d "${TEMP_DIR:-}" ]] && rm -rf "$TEMP_DIR"' EXIT
  log "Checking dependencies..."
  check_dependencies

  # cleanup local conflicting installs
  rm -f "$HOME/.local/bin/pat-sig" 2>/dev/null || true

  local archive
  archive=$(download_and_install "$INSTALL_VERSION")
  copy_project "$archive"
  create_env

  # Initialize the database (creates db.sqlite3 + tables in the installed dir).
  log "Applying database migrations..."
  "$HOME/.local/bin/pat-sig" run --migrate --no-server >/dev/null 2>&1 \
    || (cd "$PROJECT_DEST" && "$PYTHON_BIN" manage.py migrate --noinput) || true

  cat <<EOF

✅ PAT Signage installation complete

Project:
  $PROJECT_DEST

Config:
  $ENV_FILE

Next steps:
  pat-sig install     # install the systemd service
  pat-sig start
  pat-sig status

Update:
  curl -fsSL https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/scripts/install.sh | bash

Uninstall:
  pat-sig uninstall
  pipx uninstall pat-sig
  rm -rf $CONFIG_DIR

EOF
}
if [[ "${BASH_SOURCE[0]-$0}" == "$0" ]]; then
  main "$@"
fi
