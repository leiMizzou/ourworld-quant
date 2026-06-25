#!/usr/bin/env zsh
set -euo pipefail

ROOT_DIR="${OWQ_ROOT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT_DIR"

ENV_FILE="${OWQ_ENV_FILE:-deploy/public.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

PYTHON="${PYTHON:-.venv/bin/python}"
PUBLIC_BASE_URL="${OWQ_PUBLIC_BASE_URL:-https://quant.ourworlds.app}"
LOCAL_BASE_URL="${OWQ_LOCAL_BASE_URL:-http://${OWQ_HOST:-127.0.0.1}:${OWQ_PORT:-8081}}"
CHECK_TIMEOUT="${OWQ_CHECK_TIMEOUT_SECONDS:-15}"
ALLOW_PUBLIC_BETA="${OWQ_ALLOW_PUBLIC_BETA:-0}"
ALLOWED_READY_WARNINGS="${OWQ_ALLOWED_READY_WARNINGS:-email_sending,email_dev_auth_public}"
DOMAIN="${OWQ_LAUNCHD_DOMAIN:-gui/$(id -u)}"
LOG_DIR="${OWQ_LAUNCHD_LOG_DIR:-$HOME/Library/Logs/OurWorldsQuant}"
APP_LABEL="com.ourworlds.quant.app"
SYNC_LABEL="com.ourworlds.quant.market-sync"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

failures=0
warnings=0

ok() {
  print -r -- "[OK] $*"
}

warn() {
  print -r -- "[WARN] $*"
  warnings=$((warnings + 1))
}

fail() {
  print -r -- "[FAIL] $*"
  failures=$((failures + 1))
}

require_command() {
  local name="$1"
  if command -v "$name" >/dev/null 2>&1; then
    ok "command ${name}"
  else
    fail "missing command ${name}"
  fi
}

curl_capture() {
  local name="$1"
  local url="$2"
  local expected="$3"
  local body="$4"
  local headers="$5"
  local code
  local attempt
  for attempt in 1 2 3; do
    code="$(curl -sS --max-time "$CHECK_TIMEOUT" -D "$headers" -o "$body" -w "%{http_code}" "$url" 2>"${body}.curl.err" || true)"
    if [[ "$code" == "$expected" ]]; then
      ok "${name} ${url} -> ${code}"
      return 0
    fi
    if [[ "$attempt" != "3" ]]; then
      sleep 1
    fi
  done
  fail "${name} ${url} -> ${code:-000}, expected ${expected}; $(cat "${body}.curl.err" 2>/dev/null || true)"
  return 1
}

check_launchd_service() {
  local label="$1"
  local expected_state="$2"
  check_launchd_service_any "$label" "$expected_state"
}

check_launchd_service_any() {
  local label="$1"
  shift
  local output="$TMP_DIR/${label}.launchctl.txt"
  if ! command -v launchctl >/dev/null 2>&1; then
    warn "launchctl unavailable; skipped ${label}"
    return 0
  fi
  if ! launchctl print "${DOMAIN}/${label}" >"$output" 2>&1; then
    fail "launchd ${label} not installed in ${DOMAIN}"
    return 1
  fi
  local state
  state="$(awk -F'= ' '/state =/{print $2; exit}' "$output" | sed 's/^ *//;s/ *$//')"
  local expected
  for expected in "$@"; do
    if [[ "$state" == "$expected" ]]; then
      ok "launchd ${label} state=${state}"
      return 0
    fi
  done
  fail "launchd ${label} state=${state:-unknown}; expected one of: $*"
}

check_launchd_last_exit_if_ran() {
  local label="$1"
  local output="$TMP_DIR/${label}.launchctl.txt"
  if [[ ! -f "$output" ]]; then
    warn "launchd output missing for ${label}; skipped last-exit check"
    return 0
  fi
  local state
  state="$(awk -F'= ' '/state =/{print $2; exit}' "$output" | sed 's/^ *//;s/ *$//')"
  if [[ "$state" == "running" ]]; then
    ok "launchd ${label} is currently running; last-exit check deferred"
    return 0
  fi
  local runs
  runs="$(awk -F'= ' '/runs =/{gsub(/[^0-9]/, "", $2); print $2; exit}' "$output")"
  if [[ -z "$runs" || "$runs" == "0" ]]; then
    ok "launchd ${label} has not run yet"
    return 0
  fi
  if grep -q "last exit code = 0" "$output"; then
    ok "launchd ${label} last exit code=0"
    return 0
  fi
  fail "launchd ${label} last run failed: $(grep 'last exit code =' "$output" | head -n 1 | sed 's/^ *//')"
}

check_health_json() {
  local body="$1"
  CHECK_BODY="$body" "$PYTHON" - <<'PY'
import json
import os
import sys

body_path = os.environ["CHECK_BODY"]
if not os.path.exists(body_path):
    print(f"healthz body missing: {body_path}")
    sys.exit(1)
try:
    with open(body_path, encoding="utf-8") as fh:
        payload = json.load(fh)
except Exception as exc:
    print(f"healthz JSON invalid: {type(exc).__name__}: {exc}")
    sys.exit(1)
if payload.get("status") not in {"ok", "degraded"}:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(1)
if not isinstance(payload.get("checks"), list) and not isinstance(payload.get("warnings"), list):
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(1)
if "required_warnings" not in payload:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(1)
sys.exit(0)
PY
}

check_livez_json() {
  local body="$1"
  CHECK_BODY="$body" "$PYTHON" - <<'PY'
import json
import os
import sys

body_path = os.environ["CHECK_BODY"]
if not os.path.exists(body_path):
    print(f"livez body missing: {body_path}")
    sys.exit(1)
try:
    with open(body_path, encoding="utf-8") as fh:
        payload = json.load(fh)
except Exception as exc:
    print(f"livez JSON invalid: {type(exc).__name__}: {exc}")
    sys.exit(1)
if payload.get("ok") is not True or payload.get("database") != "ok":
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(1)
sys.exit(0)
PY
}

check_ready_json() {
  local body="$1"
  local http_code="$2"
  CHECK_BODY="$body" \
  CHECK_HTTP_CODE="$http_code" \
  CHECK_ALLOW_PUBLIC_BETA="$ALLOW_PUBLIC_BETA" \
  CHECK_ALLOWED_READY_WARNINGS="$ALLOWED_READY_WARNINGS" \
  "$PYTHON" - <<'PY'
import json
import os
import sys

body_path = os.environ["CHECK_BODY"]
if not os.path.exists(body_path) or os.path.getsize(body_path) == 0:
    print(json.dumps({
        "http_code": os.environ["CHECK_HTTP_CODE"],
        "error": "readyz body missing",
        "body": body_path,
    }, ensure_ascii=False, indent=2))
    sys.exit(1)
try:
    with open(body_path, encoding="utf-8") as fh:
        payload = json.load(fh)
except Exception as exc:
    print(json.dumps({
        "http_code": os.environ["CHECK_HTTP_CODE"],
        "error": f"readyz JSON invalid: {type(exc).__name__}: {exc}",
        "body": body_path,
    }, ensure_ascii=False, indent=2))
    sys.exit(1)

code = os.environ["CHECK_HTTP_CODE"]
if code == "200" and payload.get("ok") is True:
    print("formal-ready")
    sys.exit(0)

allow_beta = os.environ["CHECK_ALLOW_PUBLIC_BETA"] in {"1", "true", "yes", "on"}
allowed = {item.strip() for item in os.environ["CHECK_ALLOWED_READY_WARNINGS"].split(",") if item.strip()}
rows = payload.get("checks")
if not isinstance(rows, list):
    rows = payload.get("warnings", [])
warnings = {
    row.get("name")
    for row in rows
    if isinstance(row, dict) and row.get("status") == "warn"
}
required = int(payload.get("required_warnings") or 0)
if allow_beta and code == "503" and required == 0 and warnings.issubset(allowed):
    print("public-beta:" + ",".join(sorted(warnings)))
    sys.exit(0)

print(json.dumps({
    "http_code": code,
    "ok": payload.get("ok"),
    "required_warnings": payload.get("required_warnings"),
    "optional_warnings": payload.get("optional_warnings"),
    "warning_names": sorted(warnings),
}, ensure_ascii=False, indent=2))
sys.exit(1)
PY
}

check_metrics_json() {
  local body="$1"
  CHECK_BODY="$body" "$PYTHON" - <<'PY'
import json
import os
import sys

with open(os.environ["CHECK_BODY"], encoding="utf-8") as fh:
    payload = json.load(fh)
if payload.get("status") != "ok":
    print(json.dumps({"payload": payload}, ensure_ascii=False, indent=2))
    sys.exit(1)
if payload.get("detail") == "summary":
    sys.exit(0)
required = ["status", "uptime_seconds", "requests_total", "responses_total", "by_status"]
missing = [key for key in required if key not in payload]
if missing:
    print(json.dumps({"missing": missing, "payload": payload}, ensure_ascii=False, indent=2))
    sys.exit(1)
sys.exit(0)
PY
}

header_value() {
  local headers="$1"
  local name="$2"
  "$PYTHON" - "$headers" "$name" <<'PY'
import sys

path, name = sys.argv[1], sys.argv[2].lower()
value = ""
with open(path, encoding="utf-8", errors="ignore") as fh:
    for raw in fh:
        if raw.lower().startswith(name + ":"):
            value = raw.split(":", 1)[1].strip()
            break
print(value)
PY
}

email_confirm_probe_cleanup() {
  local token="$1"
  if [[ -z "$token" ]]; then
    return 0
  fi
  PROBE_TOKEN="$token" "$PYTHON" - <<'PY' >/dev/null 2>&1 || true
import os
from pathlib import Path

from src.app import db, services

con = db.bootstrap(Path(os.getenv("OWQ_APP_DB", "data/app.sqlite")))
try:
    services.delete_email_login_session(con, os.environ["PROBE_TOKEN"])
finally:
    con.close()
PY
}

check_public_register_page() {
  local body="$TMP_DIR/register.html"
  local headers="$TMP_DIR/register.headers"
  local code
  local attempt
  for attempt in 1 2 3; do
    : > "$body"
    : > "$headers"
    code="$(curl -sS --max-time "$CHECK_TIMEOUT" -D "$headers" -o "$body" -w "%{http_code}" "${PUBLIC_BASE_URL}/register" 2>"${body}.curl.err" || true)"
    if [[ "$code" == "200" ]]; then
      break
    fi
    if [[ "$attempt" != "3" ]]; then
      sleep 1
    fi
  done
  if [[ "$code" != "200" ]]; then
    fail "public register page -> ${code:-000}, expected 200; $(cat "${body}.curl.err" 2>/dev/null || true)"
    return 1
  fi
  if grep -q '/auth/email/confirm?token=' "$body" || grep -q '测试邮箱验证链接已生成' "$body" || grep -q '当前启用本地邮箱测试注册' "$body"; then
    fail "public register page exposes an email dev-auth verification link or test-link copy"
    return 1
  fi
  ok "public register page does not expose email dev-auth verification links"
}

head_status() {
  local url="$1"
  local headers="$2"
  local expected="$3"
  local code attempt
  for attempt in 1 2 3; do
    : > "$headers"
    code="$(curl -sS -I --max-time "$CHECK_TIMEOUT" -D "$headers" -o /dev/null -w "%{http_code}" "$url" 2>"${headers}.curl.err" || true)"
    if [[ "$code" == "$expected" ]]; then
      print -r -- "$code"
      return 0
    fi
    if [[ "$attempt" != "3" ]]; then
      sleep 1
    fi
  done
  print -r -- "${code:-000}"
  return 1
}

check_public_head_routes() {
  local headers="$TMP_DIR/head-app.headers"
  local code location route protected_route slug
  if ! code="$(head_status "${PUBLIC_BASE_URL}/app" "$headers" "303")"; then
    fail "HEAD ${PUBLIC_BASE_URL}/app -> ${code:-000}, expected 303; $(cat "${headers}.curl.err" 2>/dev/null || true)"
    return 1
  fi
  location="$(header_value "$headers" "location")"
  if [[ "$location" != "/login" ]]; then
    fail "HEAD ${PUBLIC_BASE_URL}/app redirected to ${location:-empty}, expected /login"
    return 1
  fi
  ok "public HEAD /app redirects unauthenticated users to /login"
  for protected_route in /account/consent; do
    slug="${protected_route#/}"
    slug="${slug//\//_}"
    headers="$TMP_DIR/head-protected-${slug}.headers"
    if ! code="$(head_status "${PUBLIC_BASE_URL}${protected_route}" "$headers" "303")"; then
      fail "HEAD ${PUBLIC_BASE_URL}${protected_route} -> ${code:-000}, expected 303; $(cat "${headers}.curl.err" 2>/dev/null || true)"
      continue
    fi
    location="$(header_value "$headers" "location")"
    if [[ "$location" != "/login" ]]; then
      fail "HEAD ${PUBLIC_BASE_URL}${protected_route} redirected to ${location:-empty}, expected /login"
      continue
    fi
    ok "public HEAD ${protected_route} redirects unauthenticated users to /login"
  done
  for route in /register /forgot-password /login /showcase/public /forum /terms /privacy /risk /support; do
    slug="${route#/}"
    slug="${slug//\//_}"
    headers="$TMP_DIR/head-${slug}.headers"
    if ! code="$(head_status "${PUBLIC_BASE_URL}${route}" "$headers" "200")"; then
      fail "HEAD ${PUBLIC_BASE_URL}${route} -> ${code:-000}, expected 200; $(cat "${headers}.curl.err" 2>/dev/null || true)"
      continue
    fi
    if [[ "$route" == "/support" ]]; then
      local robots_header
      robots_header="$(header_value "$headers" "x-robots-tag")"
      if [[ "$robots_header" != "noindex, nofollow" ]]; then
        fail "HEAD ${PUBLIC_BASE_URL}/support missing noindex header"
        continue
      fi
    fi
    ok "public HEAD ${route} -> 200"
  done
}

check_public_email_confirm_flow() {
  local email="deploy-check-$(date +%s)-$$@example.invalid"
  local token_file="$TMP_DIR/email-confirm-token.txt"
  local create_err="$TMP_DIR/email-confirm-create.err"
  local token=""
  if ! PROBE_EMAIL="$email" "$PYTHON" - <<'PY' >"$token_file" 2>"$create_err"; then
import os
from pathlib import Path

from src.app import db, services

con = db.bootstrap(Path(os.getenv("OWQ_APP_DB", "data/app.sqlite")))
try:
    token = services.create_email_login_session(
        con,
        os.environ["PROBE_EMAIL"],
        "2026-06-24",
        "2026-06-24",
        "2026-06-24",
        enforce_rate_limit=False,
    )
    print(token)
finally:
    con.close()
PY
    fail "email confirm probe session creation failed: $(cat "$create_err" 2>/dev/null || true)"
    return 1
  fi
  token="$(cat "$token_file" 2>/dev/null || true)"
  if [[ -z "$token" ]]; then
    fail "email confirm probe did not return a token"
    return 1
  fi

  local link_headers="$TMP_DIR/email-confirm-link.headers"
  local link_body="$TMP_DIR/email-confirm-link.body"
  local link_code
  local attempt
  for attempt in 1 2 3; do
    : > "$link_headers"
    : > "$link_body"
    link_code="$(curl -sS --max-time "$CHECK_TIMEOUT" -D "$link_headers" -o "$link_body" -w "%{http_code}" "${PUBLIC_BASE_URL}/auth/email/confirm?token=${token}" 2>"${link_body}.curl.err" || true)"
    if [[ "$link_code" == "303" ]]; then
      break
    fi
    if [[ "$attempt" != "3" ]]; then
      sleep 1
    fi
  done
  if [[ "$link_code" != "303" ]]; then
    fail "public email confirm link GET -> ${link_code:-000}, expected 303; $(cat "${link_body}.curl.err" 2>/dev/null || true)"
    email_confirm_probe_cleanup "$token"
    return 1
  fi
  local location
  location="$(header_value "$link_headers" "location")"
  if [[ "$location" != "/auth/email/confirm" ]]; then
    fail "public email confirm link did not redirect to clean confirmation URL"
    email_confirm_probe_cleanup "$token"
    return 1
  fi

  local confirm_cookie
  confirm_cookie="$("$PYTHON" - "$link_headers" <<'PY'
import sys

cookie = ""
with open(sys.argv[1], encoding="utf-8", errors="ignore") as fh:
    for raw in fh:
        if raw.lower().startswith("set-cookie:"):
            value = raw.split(":", 1)[1].strip()
            if value.startswith("owq_email_confirm="):
                cookie = value.split(";", 1)[0]
                break
print(cookie)
PY
)"
  if [[ "$confirm_cookie" != owq_email_confirm=* ]]; then
    fail "public email confirm link did not set owq_email_confirm"
    email_confirm_probe_cleanup "$token"
    return 1
  fi
  if grep -qi '^set-cookie:.*owq_session=' "$link_headers"; then
    fail "public email confirm link set a session cookie before POST"
    email_confirm_probe_cleanup "$token"
    return 1
  fi

  local confirm_headers="$TMP_DIR/email-confirm-clean.headers"
  local confirm_body="$TMP_DIR/email-confirm-clean.html"
  local confirm_code
  for attempt in 1 2 3; do
    : > "$confirm_headers"
    : > "$confirm_body"
    confirm_code="$(curl -sS --max-time "$CHECK_TIMEOUT" -H "Cookie: ${confirm_cookie}" -D "$confirm_headers" -o "$confirm_body" -w "%{http_code}" "${PUBLIC_BASE_URL}/auth/email/confirm" 2>"${confirm_body}.curl.err" || true)"
    if [[ "$confirm_code" == "200" ]]; then
      break
    fi
    if [[ "$attempt" != "3" ]]; then
      sleep 1
    fi
  done
  if [[ "$confirm_code" != "200" ]]; then
    fail "public clean email confirmation page -> ${confirm_code:-000}, expected 200; $(cat "${confirm_body}.curl.err" 2>/dev/null || true)"
    email_confirm_probe_cleanup "$token"
    return 1
  fi
  if ! grep -q '设置登录账号' "$confirm_body"; then
    fail "public clean email setup page missing setup title"
    email_confirm_probe_cleanup "$token"
    return 1
  fi
  if grep -q "$token" "$confirm_body"; then
    fail "public clean email confirmation page leaked raw token"
    email_confirm_probe_cleanup "$token"
    return 1
  fi
  if grep -qi '^set-cookie:.*owq_session=' "$confirm_headers"; then
    fail "public clean email confirmation page set a session cookie before POST"
    email_confirm_probe_cleanup "$token"
    return 1
  fi

  local state_output="$TMP_DIR/email-confirm-state.txt"
  if ! PROBE_TOKEN="$token" PROBE_EMAIL="$email" "$PYTHON" - <<'PY' >"$state_output" 2>&1; then
import os
from pathlib import Path

from src.app import db, services

con = db.bootstrap(Path(os.getenv("OWQ_APP_DB", "data/app.sqlite")))
try:
    status = services.email_login_session_status(con, os.environ["PROBE_TOKEN"])["status"]
    users = con.execute("SELECT COUNT(*) FROM users WHERE email=?", (os.environ["PROBE_EMAIL"],)).fetchone()[0]
finally:
    con.close()
if status != "pending" or users != 0:
    raise SystemExit(f"status={status} users={users}")
PY
    fail "public email confirmation GET consumed token or created user: $(cat "$state_output")"
    email_confirm_probe_cleanup "$token"
    return 1
  fi
  ok "public email verification keeps token out of account setup HTML"
  email_confirm_probe_cleanup "$token"
}

check_public_text_files() {
  local robots="$1"
  local sitemap="$2"
  if grep -q "Sitemap: ${PUBLIC_BASE_URL}/sitemap.xml" "$robots"; then
    ok "robots sitemap points at public base URL"
  else
    fail "robots.txt does not point at ${PUBLIC_BASE_URL}/sitemap.xml"
  fi
  if grep -q "Disallow: /support" "$robots"; then
    ok "robots disallows support request page"
  else
    fail "robots.txt does not disallow /support"
  fi
  for blocked in "/admin" "/register" "/login" "/support" "/livez" "/metrics" "/healthz" "/readyz"; do
    if grep -q "$blocked" "$sitemap"; then
      fail "sitemap includes internal endpoint ${blocked}"
    fi
  done
  for expected in "${PUBLIC_BASE_URL}/" "${PUBLIC_BASE_URL}/data-status" "${PUBLIC_BASE_URL}/showcase/public" "${PUBLIC_BASE_URL}/forum" "${PUBLIC_BASE_URL}/terms"; do
    if grep -q "$expected" "$sitemap"; then
      ok "sitemap includes ${expected}"
    else
      fail "sitemap missing ${expected}"
    fi
  done
}

check_public_sensitive_content() {
  local routes=(/ /register /forgot-password /login /support /data-status /showcase/public /forum /livez /metrics /healthz /readyz)
  local files=()
  local route body headers code attempt
  for route in "${routes[@]}"; do
    local slug="${route#/}"
    [[ -n "$slug" ]] || slug="root"
    slug="${slug//\//_}"
    body="$TMP_DIR/sensitive-${slug}.body"
    headers="$TMP_DIR/sensitive-${slug}.headers"
    for attempt in 1 2 3; do
      code="$(curl -sS --max-time "$CHECK_TIMEOUT" -D "$headers" -o "$body" -w "%{http_code}" "${PUBLIC_BASE_URL}${route}" 2>"${body}.curl.err" || true)"
      if [[ "$route" == "/healthz" || "$route" == "/readyz" ]]; then
        [[ "$code" == "200" || "$code" == "503" ]] && break
      elif [[ "$code" == "200" ]]; then
        break
      fi
      if [[ "$attempt" != "3" ]]; then
        sleep 1
      fi
    done
    if [[ "$route" == "/healthz" || "$route" == "/readyz" ]]; then
      if [[ "$code" != "200" && "$code" != "503" ]]; then
        fail "sensitive scan fetch ${route} -> ${code:-000}, expected 200 or 503"
        continue
      fi
    elif [[ "$code" != "200" ]]; then
      fail "sensitive scan fetch ${route} -> ${code:-000}, expected 200"
      continue
    fi
    files+=("$body")
  done
  if (( ${#files[@]} == 0 )); then
    fail "sensitive scan had no response bodies"
    return 1
  fi
  if SCAN_FILES="${(j:,:)files}" SCAN_HOME="$HOME" "$PYTHON" - <<'PY'
import os
import sys
from pathlib import Path

secret_names = [
    "OWQ_SECRET",
    "CLOUDFLARE_API_TOKEN",
    "OWQ_SMTP_PASSWORD",
    "TUSHARE_TOKEN",
    "OWQ_SET_PASSWORD",
]
secrets = []
for name in secret_names:
    value = os.getenv(name, "").strip()
    if value and len(value) >= 8:
        secrets.append((name, value))
secret_file = os.getenv("OWQ_SECRET_FILE", "").strip()
if secret_file:
    try:
        value = Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value and len(value) >= 8:
        secrets.append(("OWQ_SECRET_FILE", value))

local_markers = ["/Volumes/EXTDISK/", "/Users/leihua/"]
home = os.getenv("SCAN_HOME", "").strip()
if home and len(home) > 6:
    local_markers.append(home.rstrip("/") + "/")

failures = []
for raw_path in os.getenv("SCAN_FILES", "").split(","):
    if not raw_path:
        continue
    try:
        text = Path(raw_path).read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        failures.append(f"{raw_path}: read failed {type(exc).__name__}")
        continue
    for name, value in secrets:
        if value in text:
            failures.append(f"{raw_path}: leaked secret value from {name}")
    for marker in local_markers:
        if marker in text:
            failures.append(f"{raw_path}: leaked local path marker {marker}")

if failures:
    print("\n".join(failures))
    sys.exit(1)
PY
  then
    ok "public response bodies do not expose configured secret values or local paths"
  else
    fail "public sensitive response scan failed"
  fi
}

check_log_file() {
  local log_path="$1"
  if [[ ! -e "$log_path" ]]; then
    warn "log file missing: ${log_path}"
    return 0
  fi
  if [[ -s "$log_path" ]]; then
    warn "log file is non-empty: ${log_path}; inspect with tail -n 100"
  else
    ok "log file has no current stderr content: ${log_path}"
  fi
}

check_latest_backup_verify() {
  local backup_dir="${OWQ_APP_BACKUP_DIR:-data/backups}"
  local latest
  latest="$(ls -t "${backup_dir}"/app-*.sqlite 2>/dev/null | head -n 1 || true)"
  if [[ -z "$latest" ]]; then
    fail "no app backup found in ${backup_dir}"
    return 1
  fi
  local output="$TMP_DIR/backup-verify.txt"
  if "$PYTHON" -m src.app.server --env-file "$ENV_FILE" --verify-app-backup "$latest" >"$output" 2>&1; then
    ok "latest app backup verifies: $(basename "$latest")"
  else
    fail "latest app backup verification failed: $(cat "$output")"
  fi
}

check_latest_backup_restore_drill() {
  local backup_dir="${OWQ_APP_BACKUP_DIR:-data/backups}"
  local latest
  latest="$(ls -t "${backup_dir}"/app-*.sqlite 2>/dev/null | head -n 1 || true)"
  if [[ -z "$latest" ]]; then
    fail "no app backup found for restore drill in ${backup_dir}"
    return 1
  fi
  local target="$TMP_DIR/restore-drill.sqlite"
  local output="$TMP_DIR/backup-restore.txt"
  if "$PYTHON" -m src.app.server --env-file "$ENV_FILE" --restore-app-backup "$latest" "$target" >"$output" 2>&1; then
    ok "latest app backup restore drill passed: $(basename "$latest")"
  else
    fail "latest app backup restore drill failed: $(cat "$output")"
  fi
}

print -r -- "Checking OurWorlds Quant public deployment"
print -r -- "public=${PUBLIC_BASE_URL}"
print -r -- "local=${LOCAL_BASE_URL}"

require_command curl
require_command "$PYTHON"

check_launchd_service "$APP_LABEL" "running"
check_launchd_service_any "$SYNC_LABEL" "not running" "running"
check_launchd_last_exit_if_ran "$SYNC_LABEL"

live_body="$TMP_DIR/livez.json"
live_headers="$TMP_DIR/livez.headers"
if curl_capture "local livez" "${LOCAL_BASE_URL}/livez" "200" "$live_body" "$live_headers"; then
  if check_livez_json "$live_body"; then
    ok "local livez JSON ok"
  else
    fail "local livez JSON is not ok"
  fi
fi

public_live_body="$TMP_DIR/public-livez.json"
public_live_headers="$TMP_DIR/public-livez.headers"
if curl_capture "public livez" "${PUBLIC_BASE_URL}/livez" "200" "$public_live_body" "$public_live_headers"; then
  if check_livez_json "$public_live_body"; then
    ok "public livez JSON ok"
  else
    fail "public livez JSON is not ok"
  fi
fi

health_body="$TMP_DIR/health.json"
health_headers="$TMP_DIR/health.headers"
health_code="$(curl -sS --max-time "$CHECK_TIMEOUT" -D "$health_headers" -o "$health_body" -w "%{http_code}" "${LOCAL_BASE_URL}/healthz" 2>"${health_body}.curl.err" || true)"
if [[ "$health_code" == "200" || "$health_code" == "503" ]]; then
  if check_health_json "$health_body"; then
    ok "local health JSON ok (${health_code})"
  else
    fail "local health JSON is not valid"
  fi
else
  fail "local health ${LOCAL_BASE_URL}/healthz -> ${health_code:-000}, expected 200 or 503; $(cat "${health_body}.curl.err" 2>/dev/null || true)"
fi

public_health_body="$TMP_DIR/public-health.json"
public_health_headers="$TMP_DIR/public-health.headers"
public_health_code="$(curl -sS --max-time "$CHECK_TIMEOUT" -D "$public_health_headers" -o "$public_health_body" -w "%{http_code}" "${PUBLIC_BASE_URL}/healthz" 2>"${public_health_body}.curl.err" || true)"
if [[ "$public_health_code" == "200" || "$public_health_code" == "503" ]]; then
  if check_health_json "$public_health_body"; then
    ok "public health JSON ok (${public_health_code})"
  else
    fail "public health JSON is not valid"
  fi
else
  fail "public health ${PUBLIC_BASE_URL}/healthz -> ${public_health_code:-000}, expected 200 or 503; $(cat "${public_health_body}.curl.err" 2>/dev/null || true)"
fi

ready_body="$TMP_DIR/ready.json"
ready_headers="$TMP_DIR/ready.headers"
ready_code=""
for attempt in 1 2 3; do
  : > "$ready_body"
  : > "$ready_headers"
  ready_code="$(curl -sS --max-time "$CHECK_TIMEOUT" -D "$ready_headers" -o "$ready_body" -w "%{http_code}" "${PUBLIC_BASE_URL}/readyz" 2>"${ready_body}.curl.err" || true)"
  if [[ ( "$ready_code" == "200" || "$ready_code" == "503" ) && -s "$ready_body" ]]; then
    break
  fi
  if [[ "$attempt" != "3" ]]; then
    sleep 1
  fi
done
touch "$ready_body"
ready_result="$(check_ready_json "$ready_body" "$ready_code" 2>"$TMP_DIR/ready-check.err" || true)"
if [[ "$ready_result" == "formal-ready" ]]; then
  ok "public readyz formal gate passed"
elif [[ "$ready_result" == public-beta:* ]]; then
  ok "public readyz accepted for beta with warnings ${ready_result#public-beta:}"
else
  fail "public readyz gate failed: ${ready_result} $(cat "$TMP_DIR/ready-check.err" 2>/dev/null || true)"
fi

metrics_body="$TMP_DIR/metrics.json"
metrics_headers="$TMP_DIR/metrics.headers"
if curl_capture "public metrics" "${PUBLIC_BASE_URL}/metrics" "200" "$metrics_body" "$metrics_headers"; then
  if check_metrics_json "$metrics_body"; then
    ok "public metrics JSON ok"
  else
    fail "public metrics JSON invalid"
  fi
fi

data_status_body="$TMP_DIR/data-status.html"
data_status_headers="$TMP_DIR/data-status.headers"
curl_capture "public data status" "${PUBLIC_BASE_URL}/data-status" "200" "$data_status_body" "$data_status_headers" || true

robots_body="$TMP_DIR/robots.txt"
robots_headers="$TMP_DIR/robots.headers"
sitemap_body="$TMP_DIR/sitemap.xml"
sitemap_headers="$TMP_DIR/sitemap.headers"
curl_capture "public robots" "${PUBLIC_BASE_URL}/robots.txt" "200" "$robots_body" "$robots_headers" || true
curl_capture "public sitemap" "${PUBLIC_BASE_URL}/sitemap.xml" "200" "$sitemap_body" "$sitemap_headers" || true
if [[ -s "$robots_body" && -s "$sitemap_body" ]]; then
  check_public_text_files "$robots_body" "$sitemap_body"
fi

check_public_sensitive_content
check_public_register_page
check_public_head_routes
check_public_email_confirm_flow
check_latest_backup_verify
check_latest_backup_restore_drill
check_log_file "${LOG_DIR}/app.err.log"

if (( failures > 0 )); then
  print -r -- "Deployment check failed: ${failures} failure(s), ${warnings} warning(s)."
  exit 1
fi

print -r -- "Deployment check passed with ${warnings} warning(s)."
