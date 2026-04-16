#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./deploy/push.sh <user@host> [remote_dir] [--no-delete] [--no-sudo] [--port N]

Examples:
  ./deploy/push.sh waysker@orbit
  ./deploy/push.sh waysker@orbit /opt/teatr-bilety-watcher --port 22
  ./deploy/push.sh waysker@orbit /opt/teatr-bilety-watcher --no-delete
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

REMOTE=""
REMOTE_DIR="/opt/teatr-bilety-watcher"
USE_DELETE=1
USE_SUDO=1
SSH_PORT=""

POSITIONAL_INDEX=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-delete)
      USE_DELETE=0
      shift
      ;;
    --no-sudo)
      USE_SUDO=0
      shift
      ;;
    --port)
      SSH_PORT="${2:-}"
      if [[ -z "$SSH_PORT" ]]; then
        echo "Missing value for --port"
        exit 1
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      POSITIONAL_INDEX=$((POSITIONAL_INDEX + 1))
      if [[ $POSITIONAL_INDEX -eq 1 ]]; then
        REMOTE="$1"
      elif [[ $POSITIONAL_INDEX -eq 2 ]]; then
        REMOTE_DIR="$1"
      else
        echo "Unexpected argument: $1"
        usage
        exit 1
      fi
      shift
      ;;
  esac
done

if [[ "$REMOTE" != *@* ]]; then
  echo "Remote must be in user@host format."
  exit 1
fi

REMOTE_USER="${REMOTE%@*}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SSH_CMD=(ssh)
RSYNC_SSH="ssh"
if [[ -n "$SSH_PORT" ]]; then
  SSH_CMD+=(-p "$SSH_PORT")
  RSYNC_SSH="ssh -p $SSH_PORT"
fi

echo "Preparing remote directory: $REMOTE:$REMOTE_DIR"
if [[ "$USE_SUDO" -eq 1 ]]; then
  "${SSH_CMD[@]}" "$REMOTE" "sudo mkdir -p '$REMOTE_DIR' && sudo chown -R '$REMOTE_USER':'$REMOTE_USER' '$REMOTE_DIR'"
else
  "${SSH_CMD[@]}" "$REMOTE" "mkdir -p '$REMOTE_DIR'"
fi

RSYNC_CMD=(rsync -az)
if [[ "$USE_DELETE" -eq 1 ]]; then
  RSYNC_CMD+=(--delete)
fi
RSYNC_CMD+=(
  --exclude ".git/"
  --exclude ".venv/"
  --exclude "__pycache__/"
  --exclude ".env"
  --exclude "data/"
  --exclude "*.pyc"
  --exclude ".DS_Store"
  -e "$RSYNC_SSH"
  "$ROOT_DIR/"
  "$REMOTE:$REMOTE_DIR/"
)

echo "Syncing project files..."
"${RSYNC_CMD[@]}"

cat <<EOF
Done.

Next:
  ssh $REMOTE "cd $REMOTE_DIR && ./deploy/install.sh --app-user $REMOTE_USER --app-group $REMOTE_USER"
EOF
