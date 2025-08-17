#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/xsmom-bot"
SERVICE_NAME="xsmom-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="/usr/bin/python3"

# You can override the run user by exporting RUN_AS before calling install.sh
RUN_AS="${RUN_AS:-ubuntu}"
RUN_GROUP="${RUN_GROUP:-$RUN_AS}"

echo "[*] Creating app directory at ${APP_DIR}..."
sudo mkdir -p "${APP_DIR}"
sudo mkdir -p "${APP_DIR}/src" "${APP_DIR}/systemd" "${APP_DIR}/state" "${APP_DIR}/logs"

echo "[*] Copying repository files..."
# Assumes you run this from the repo root
sudo rsync -a --delete \
  README.md requirements.txt .env.example install.sh run_local.sh \
  src/ systemd/ state/ logs/ \
  "${APP_DIR}/"

echo "[*] Setting permissions..."
sudo chown -R "${RUN_AS}:${RUN_GROUP}" "${APP_DIR}"

echo "[*] Creating Python venv and installing requirements..."
sudo -u "${RUN_AS}" ${PYTHON_BIN} -m venv "${APP_DIR}/venv"
sudo -u "${RUN_AS}" "${APP_DIR}/venv/bin/pip" install --upgrade pip
sudo -u "${RUN_AS}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "[*] Creating .env from example (if missing)..."
if [ ! -f "${APP_DIR}/.env" ]; then
  sudo -u "${RUN_AS}" cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo "    -> Edit ${APP_DIR}/.env to set your BYBIT_API_KEY/SECRET"
fi

echo "[*] Installing systemd service..."
sudo cp "${APP_DIR}/systemd/${SERVICE_NAME}.service" "${SERVICE_FILE}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

echo "[*] Start service now? (y/N)"
read -r yn
if [[ "${yn,,}" == "y" ]]; then
  sudo systemctl start "${SERVICE_NAME}"
  echo "[*] Service started. Tail logs with:"
  echo "    journalctl -u ${SERVICE_NAME} -f -o cat"
else
  echo "[*] You can start later with: sudo systemctl start ${SERVICE_NAME}"
fi

echo "[*] Done."
