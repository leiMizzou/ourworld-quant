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

PYTHON="${PYTHON:-.venv/bin/python}"
DATA_SOURCE="${OWQ_MARKET_SOURCE:-tushare}"
DATA_ADJUST="${OWQ_MARKET_DATA_ADJUST:-none}"
APP_ADJUST="${OWQ_MARKET_APP_ADJUST:-none}"
MARKET_LIMIT="${OWQ_MARKET_LIMIT:-500}"
SYNC_DATA_FIRST="${OWQ_SYNC_DATA_FIRST:-0}"
SYNC_STOCKS="${OWQ_SYNC_STOCKS:-1}"
SYNC_REPORTS="${OWQ_SYNC_REPORTS:-1}"
SYNC_PRUNE_AUDIT="${OWQ_SYNC_PRUNE_AUDIT:-1}"
SYNC_PRUNE_EMAIL_LOGIN="${OWQ_SYNC_PRUNE_EMAIL_LOGIN:-1}"
STRICT_READY="${OWQ_SYNC_STRICT_READY:-0}"

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
export OWQ_DB_PATH="${OWQ_DB_PATH:-data/market.duckdb}"
if [[ "$DATA_SOURCE" == "tushare" ]]; then
  export OWQ_SLEEP="${OWQ_SLEEP:-1.3}"
fi

record_sync_status() {
  local sync_state="$1"
  local exit_code="${2:-0}"
  "$PYTHON" -m src.app.server \
    --record-market-sync-status "$sync_state" \
    --market-sync-exit-code "$exit_code" \
    --market-sync-message "deploy/sync-market-public.sh" >/dev/null 2>&1 || true
}

on_exit() {
  local exit_code="$?"
  if [[ "$exit_code" == "0" ]]; then
    record_sync_status succeeded "$exit_code"
  else
    record_sync_status failed "$exit_code"
  fi
}

record_sync_status started 0
trap on_exit EXIT

if [[ "$SYNC_DATA_FIRST" == "1" ]]; then
  "$PYTHON" -m src.data.cli init
  if [[ "$SYNC_STOCKS" == "1" ]]; then
    "$PYTHON" -m src.data.cli stocks --source "$DATA_SOURCE"
  fi
  "$PYTHON" -m src.data.cli daily \
    --source "$DATA_SOURCE" \
    --adjust "$DATA_ADJUST" \
    --start "${OWQ_START:-20180101}" \
    --limit "$MARKET_LIMIT"
fi

"$PYTHON" -m src.app.server --backup-app-db
"$PYTHON" -m src.app.server \
  --sync-market \
  --market-adjust "$APP_ADJUST" \
  --market-limit "$MARKET_LIMIT" \
  --replace-market \
  --sync-only

if [[ "$SYNC_REPORTS" == "1" ]]; then
  "$PYTHON" -m src.research.real_data_report \
    --start "${OWQ_REPORT_START:-20230101}" \
    --adjust "${OWQ_REPORT_ADJUST:-hfq}" \
    --top "${OWQ_REPORT_TOP:-20}" \
    --app-db "$OWQ_APP_DB" \
    --out "${OWQ_REPORT_OUT:-reports/real-data-report.md}" \
    --predictions-csv "${OWQ_PREDICTIONS_CSV:-reports/predictions.csv}"
fi

"$PYTHON" -m src.app.server --sqlite-maintenance

if [[ "$SYNC_PRUNE_AUDIT" == "1" ]]; then
  "$PYTHON" -m src.app.server --prune-audit-log
fi

if [[ "$SYNC_PRUNE_EMAIL_LOGIN" == "1" ]]; then
  "$PYTHON" -m src.app.server --prune-email-login-sessions
fi

if [[ "$STRICT_READY" == "1" ]]; then
  "$PYTHON" -m src.app.server --doctor-strict
else
  "$PYTHON" -m src.app.server --doctor || true
fi
