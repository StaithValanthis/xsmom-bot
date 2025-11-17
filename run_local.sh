#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${APP_DIR}/venv/bin/activate"

if [ -f "${APP_DIR}/.env" ]; then
  set -a
  source "${APP_DIR}/.env"
  set +a
fi

# Backtest with current config
python -m src.main backtest --config "${APP_DIR}/config/config.yaml"
