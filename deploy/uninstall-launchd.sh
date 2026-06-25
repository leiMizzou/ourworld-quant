#!/usr/bin/env zsh
set -euo pipefail

DOMAIN="${OWQ_LAUNCHD_DOMAIN:-gui/$(id -u)}"
AGENT_DIR="${OWQ_LAUNCHD_AGENT_DIR:-$HOME/Library/LaunchAgents}"
SUPPORT_DIR="${OWQ_LAUNCHD_SUPPORT_DIR:-$HOME/Library/Application Support/OurWorldsQuant}"
LABELS=(
  "com.ourworlds.quant.app"
  "com.ourworlds.quant.market-sync"
)

for label in "${LABELS[@]}"; do
  launchctl bootout "${DOMAIN}/${label}" >/dev/null 2>&1 || true
  rm -f "${AGENT_DIR}/${label}.plist"
done
rm -f "${SUPPORT_DIR}/run-public-app.sh" "${SUPPORT_DIR}/sync-market-public.sh" "${SUPPORT_DIR}/public.env" "${SUPPORT_DIR}/app.secret"

echo "launchd agents removed from ${AGENT_DIR}"
