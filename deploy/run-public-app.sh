#!/usr/bin/env zsh
set -euo pipefail

ROOT_DIR="${OWQ_ROOT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT_DIR"

RUNTIME_SECRET_FILE="${OWQ_SECRET_FILE:-}"
ENV_FILE="${OWQ_ENV_FILE:-deploy/public.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi
if [[ -n "$RUNTIME_SECRET_FILE" ]]; then
  export OWQ_SECRET_FILE="$RUNTIME_SECRET_FILE"
fi

mkdir -p data data/backups data/logs
SECRET_FILE="${OWQ_SECRET_FILE:-data/app.secret}"
if [[ ! -s "$SECRET_FILE" ]]; then
  umask 077
  openssl rand -hex 32 > "$SECRET_FILE"
fi
chmod 600 "$SECRET_FILE"

export OWQ_ENV="${OWQ_ENV:-production}"
export OWQ_PUBLIC_BASE_URL="${OWQ_PUBLIC_BASE_URL:-https://quant.ourworlds.app}"
export OWQ_COOKIE_SECURE="${OWQ_COOKIE_SECURE:-1}"
export OWQ_SECRET="${OWQ_SECRET:-$(cat "$SECRET_FILE")}"
export OWQ_APP_DB="${OWQ_APP_DB:-data/app.sqlite}"
export OWQ_APP_BACKUP_DIR="${OWQ_APP_BACKUP_DIR:-data/backups}"
export OWQ_HOST="${OWQ_HOST:-127.0.0.1}"
export OWQ_PORT="${OWQ_PORT:-8081}"

exec .venv/bin/python -m src.app.server --host "$OWQ_HOST" --port "$OWQ_PORT"
