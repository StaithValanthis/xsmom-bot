#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${APP_DIR}/venv/bin/activate"

if [ -f "${APP_DIR}/.env" ]; then
  set -a
  source "${APP_DIR}/.env"
  set +a
fi

python -u "${APP_DIR}/src/multi_pair_xsmom.py" --backtest 1200
# Or try live dry-run:
# python -u "${APP_DIR}/src/multi_pair_xsmom.py" --live --dry
