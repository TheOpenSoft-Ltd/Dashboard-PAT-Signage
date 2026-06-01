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

install_dependencies_linux() {
  detect_linux_distro
  log "Detected Linux ($DISTRO_ID)"
  case "$DISTRO_ID" in
    ubuntu|debian|pop|linuxmint|elementary)
      UPDATE_CMD="sudo apt update"
      INSTALL_CMD="sudo apt install -y"
      PACKAGES=(python3 python3-pip python3-venv git pipx curl tar)
      ;;
    fedora|rhel|centos|rocky|almalinux)
      UPDATE_CMD="sudo dnf check-update || true"
      INSTALL_CMD="sudo dnf install -y"
      PACKAGES=(python3 python3-pip git pipx curl tar)
      ;;
    arch|archarm|manjaro|endeavouros)
      UPDATE_CMD="sudo pacman -Sy"
      INSTALL_CMD="sudo pacman -S --noconfirm"
      PACKAGES=(python python-pip git python-pipx curl tar)
      ;;
    alpine)
      UPDATE_CMD="sudo apk update"
      INSTALL_CMD="sudo apk add"
      PACKAGES=(python3 py3-pip git pipx curl tar)
      ;;
    *)
      error "Unsupported Linux distribution: $DISTRO_ID"
      ;;
  esac
  log "Installing dependencies..."
  eval "$UPDATE_CMD"
  $INSTALL_CMD "${PACKAGES[@]}"

  if command -v pipx >/dev/null 2>&1; then
    pipx ensurepath >/dev/null 2>&1 || true
  fi
  log "Dependencies installed successfully"
}

check_dependencies() {
  local missing=false
  command -v python3 >/dev/null 2>&1 || missing=true
  command -v git >/dev/null 2>&1 || missing=true
  command -v tar >/dev/null 2>&1 || missing=true
  command -v pipx >/dev/null 2>&1 || missing=true

  if [[ "$missing" == "true" ]]; then
    if [[ "$AUTO_INSTALL" == "true" ]]; then
      install_dependencies_linux
    else
      error "Missing required dependencies"
    fi
  fi
  log "All dependencies available"
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
  local version_number="${version_tag#v}"
  local url="https://github.com/$REPO_OWNER/$REPO_NAME/releases/download/$version_tag/pat_sig-${version_number}.tar.gz"
  TEMP_DIR=$(mktemp -d)

  local archive="$TEMP_DIR/pat_sig.tar.gz"

  log "Downloading release..."
  curl -fsSL -o "$archive" "$url" || error "Download failed"

  # remove old version
  if pipx list | grep -q "pat-sig"; then
    log "Removing old PAT Signage version..."
    pipx uninstall pat-sig >/dev/null 2>&1 || true
  fi

  log "Installing PAT Signage..."
  pipx install "$archive" >&2 || error "pipx install failed"
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
  # Preserve an existing .env + db.sqlite3 across upgrades.
  cp -r "$project_src"/. "$PROJECT_DEST/"
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
  pat-sig run --migrate --no-server >/dev/null 2>&1 \
    || (cd "$PROJECT_DEST" && python3 manage.py migrate --noinput) || true

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
