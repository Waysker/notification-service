#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./deploy/install.sh [--app-dir DIR] [--app-user USER] [--app-group GROUP] [--skip-enable]

Examples:
  ./deploy/install.sh --app-user waysker --app-group waysker
  ./deploy/install.sh --app-dir /opt/teatr-bilety-watcher --app-user waysker --skip-enable
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_DIR="$DEFAULT_APP_DIR"
APP_USER="${SUDO_USER:-$USER}"
APP_GROUP="$APP_USER"
ENABLE_TIMERS=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      APP_DIR="${2:-}"
      if [[ -z "$APP_DIR" ]]; then
        echo "Missing value for --app-dir"
        exit 1
      fi
      shift 2
      ;;
    --app-user)
      APP_USER="${2:-}"
      if [[ -z "$APP_USER" ]]; then
        echo "Missing value for --app-user"
        exit 1
      fi
      shift 2
      ;;
    --app-group)
      APP_GROUP="${2:-}"
      if [[ -z "$APP_GROUP" ]]; then
        echo "Missing value for --app-group"
        exit 1
      fi
      shift 2
      ;;
    --skip-enable)
      ENABLE_TIMERS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unexpected argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ ! -d "$APP_DIR" ]]; then
  echo "App directory does not exist: $APP_DIR"
  exit 1
fi

cd "$APP_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Creating virtualenv..."
  if ! python3 -m venv .venv; then
    PY_MM="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    echo "python${PY_MM}-venv is missing. Installing..."
    sudo apt update
    sudo apt install -y "python${PY_MM}-venv"
    python3 -m venv .venv
  fi
fi

source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [[ ! -f ".env" ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

echo "Installing systemd units..."
sudo cp deploy/teatr-bilety.service /etc/systemd/system/
sudo cp deploy/teatr-bilety.timer /etc/systemd/system/
sudo cp deploy/teatr-bilety-smoke.service /etc/systemd/system/
sudo cp deploy/teatr-bilety-smoke.timer /etc/systemd/system/

sudo sed -i "s/^User=.*/User=$APP_USER/" /etc/systemd/system/teatr-bilety.service /etc/systemd/system/teatr-bilety-smoke.service
sudo sed -i "s/^Group=.*/Group=$APP_GROUP/" /etc/systemd/system/teatr-bilety.service /etc/systemd/system/teatr-bilety-smoke.service

sudo systemctl daemon-reload

if [[ "$ENABLE_TIMERS" -eq 1 ]]; then
  sudo systemctl enable --now teatr-bilety.timer
  sudo systemctl enable --now teatr-bilety-smoke.timer
fi

cat <<EOF
Install finished.

Project dir: $APP_DIR
Systemd user/group: $APP_USER:$APP_GROUP

Useful checks:
  systemctl list-timers | rg teatr-bilety
  journalctl -u teatr-bilety.service -n 200 --no-pager
  journalctl -u teatr-bilety-smoke.service -n 200 --no-pager
EOF
