#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_NAME="proxyfleet-xui-sync"
readonly INSTALL_DIR="/usr/local/lib/${APP_NAME}"
readonly ENV_FILE="/etc/${APP_NAME}.env"
readonly STATE_DIR="/var/lib/${APP_NAME}"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root: sudo ./install.sh" >&2
  exit 1
fi

for command in python3 systemctl flock install; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "Missing required command: ${command}" >&2
    exit 1
  fi
done

python3 -m py_compile "${SCRIPT_DIR}/proxyfleet-xui-sync.py"

install -d -m 0755 "${INSTALL_DIR}"
install -d -m 0700 "${STATE_DIR}"
install -m 0750 \
  "${SCRIPT_DIR}/proxyfleet-xui-sync.py" \
  "${INSTALL_DIR}/proxyfleet-xui-sync.py"

install -m 0644 \
  "${SCRIPT_DIR}/systemd/proxyfleet-xui-sync.service" \
  "/etc/systemd/system/proxyfleet-xui-sync.service"
install -m 0644 \
  "${SCRIPT_DIR}/systemd/proxyfleet-xui-sync.timer" \
  "/etc/systemd/system/proxyfleet-xui-sync.timer"

if [[ ! -e "${ENV_FILE}" ]]; then
  install -m 0600 \
    "${SCRIPT_DIR}/proxyfleet-xui-sync.env.example" \
    "${ENV_FILE}"
  echo "Created ${ENV_FILE}. Add PROXYFLEET_OUTBOUNDS_TOKEN if required."
else
  chmod 0600 "${ENV_FILE}"
  while IFS= read -r line; do
    if [[ ! "${line}" =~ ^[A-Z0-9_]+= ]]; then
      continue
    fi
    key="${line%%=*}"
    if ! grep -q "^${key}=" "${ENV_FILE}"; then
      printf '\n%s\n' "${line}" >>"${ENV_FILE}"
    fi
  done <"${SCRIPT_DIR}/proxyfleet-xui-sync.env.example"
  echo "Preserved existing values and added new defaults to ${ENV_FILE}."
fi

systemctl daemon-reload
systemctl enable --now proxyfleet-xui-sync.timer
systemctl start proxyfleet-xui-sync.service

echo
echo "ProxyFleet XUI Sync installed successfully."
echo "Timer: systemctl status proxyfleet-xui-sync.timer"
echo "Logs:  journalctl -u proxyfleet-xui-sync.service -n 100 --no-pager"
