#!/usr/bin/env zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

DOMAIN="${OWQ_LAUNCHD_DOMAIN:-gui/$(id -u)}"
AGENT_DIR="${OWQ_LAUNCHD_AGENT_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${OWQ_LAUNCHD_LOG_DIR:-$HOME/Library/Logs/OurWorldsQuant}"
SUPPORT_DIR="${OWQ_LAUNCHD_SUPPORT_DIR:-$HOME/Library/Application Support/OurWorldsQuant}"
INSTALL_MARKET_SYNC="${OWQ_INSTALL_MARKET_SYNC:-0}"

APP_LABEL="com.ourworlds.quant.app"
SYNC_LABEL="com.ourworlds.quant.market-sync"

mkdir -p "$AGENT_DIR" "$LOG_DIR" "$SUPPORT_DIR" data/logs

if [[ ! -f deploy/public.env ]]; then
  cp deploy/public.env.example deploy/public.env
  chmod 600 deploy/public.env
  echo "Created deploy/public.env from deploy/public.env.example; edit it before formal production."
fi
chmod 600 deploy/public.env
cp -f deploy/public.env "${SUPPORT_DIR}/public.env"
chmod 600 "${SUPPORT_DIR}/public.env"
xattr -c "${SUPPORT_DIR}/public.env" 2>/dev/null || true
if [[ ! -s data/app.secret ]]; then
  umask 077
  openssl rand -hex 32 > data/app.secret
fi
chmod 600 data/app.secret
cp -f data/app.secret "${SUPPORT_DIR}/app.secret"
chmod 600 "${SUPPORT_DIR}/app.secret"
xattr -c "${SUPPORT_DIR}/app.secret" 2>/dev/null || true

install_agent() {
  local label="$1"
  local src="deploy/launchd/${label}.plist"
  local dest="${AGENT_DIR}/${label}.plist"
  local short_name="${label#com.ourworlds.quant.}"
  local script_name="run-public-app.sh"
  if [[ "$short_name" == "market-sync" ]]; then
    script_name="sync-market-public.sh"
  fi
  local launcher="${SUPPORT_DIR}/${script_name}"
  plutil -lint "$src" >/dev/null
  cp -f "deploy/${script_name}" "$launcher"
  chmod 755 "$launcher"
  xattr -c "$launcher" 2>/dev/null || true
  cp -f "$src" "$dest"
  /usr/libexec/PlistBuddy -c "Delete :ProgramArguments" "$dest" >/dev/null
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$dest" >/dev/null
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string /bin/zsh" "$dest" >/dev/null
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string ${launcher}" "$dest" >/dev/null
  /usr/libexec/PlistBuddy -c "Set :StandardOutPath ${LOG_DIR}/${short_name}.out.log" "$dest" >/dev/null
  /usr/libexec/PlistBuddy -c "Set :StandardErrorPath ${LOG_DIR}/${short_name}.err.log" "$dest" >/dev/null
  /usr/libexec/PlistBuddy -c "Set :WorkingDirectory /tmp" "$dest" >/dev/null
  /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OWQ_ROOT_DIR ${ROOT_DIR}" "$dest" >/dev/null 2>&1 \
    || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OWQ_ROOT_DIR string ${ROOT_DIR}" "$dest" >/dev/null
  /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OWQ_ENV_FILE ${SUPPORT_DIR}/public.env" "$dest" >/dev/null 2>&1 \
    || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OWQ_ENV_FILE string ${SUPPORT_DIR}/public.env" "$dest" >/dev/null
  /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OWQ_SECRET_FILE ${SUPPORT_DIR}/app.secret" "$dest" >/dev/null 2>&1 \
    || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OWQ_SECRET_FILE string ${SUPPORT_DIR}/app.secret" "$dest" >/dev/null
  chmod 644 "$dest"
  xattr -c "$dest" 2>/dev/null || true
  plutil -lint "$dest" >/dev/null
  launchctl bootout "${DOMAIN}/${label}" >/dev/null 2>&1 || true
  for attempt in 1 2 3; do
    if [[ "$attempt" == "3" ]]; then
      launchctl bootstrap "$DOMAIN" "$dest"
      return $?
    fi
    if launchctl bootstrap "$DOMAIN" "$dest" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    launchctl bootout "${DOMAIN}/${label}" >/dev/null 2>&1 || true
  done
}

install_agent "$APP_LABEL"
launchctl kickstart -k "${DOMAIN}/${APP_LABEL}" >/dev/null 2>&1 || true

if [[ "$INSTALL_MARKET_SYNC" == "1" ]]; then
  install_agent "$SYNC_LABEL"
fi

echo "launchd installed for $APP_LABEL"
if [[ "$INSTALL_MARKET_SYNC" == "1" ]]; then
  echo "launchd installed for $SYNC_LABEL"
else
  echo "market sync launchd not installed; set OWQ_INSTALL_MARKET_SYNC=1 when deploy/public.env has data-source credentials."
fi
