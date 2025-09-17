#!/usr/bin/env bash
# run-optimizer.sh — orchestrates PnL ingestion and the optimizer
# Usage: run-optimizer.sh /path/to/config.yaml /path/to/reports_dir
# This script is idempotent and safe to run from systemd.
set -euo pipefail

CONFIG="${1:-/opt/xsmom-bot/config/config.yaml}"
REPORTS_DIR="${2:-/opt/xsmom-bot/reports}"

# venv python (can be overridden via env)
export PYTHON_BIN="${PYTHON_BIN:-/opt/xsmom-bot/venv/bin/python}"
export PYTHONPATH="${PYTHONPATH:-/opt/xsmom-bot}"

# Load .env if present (Bybit API keys, etc.)
if [[ -f "/opt/xsmom-bot/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  source "/opt/xsmom-bot/.env"
  set +a
fi

echo "[run-optimizer] Using interpreter: ${PYTHON_BIN}"
echo "[run-optimizer] Using CONFIG: ${CONFIG}"
echo "[run-optimizer] Reports dir: ${REPORTS_DIR}"

mkdir -p "${REPORTS_DIR}"

# Try to create a writable log path (prefer /var/log, fallback to /opt)
LOGCSV="/var/log/xsmom-optimizer/history.csv"
if ! mkdir -p "/var/log/xsmom-optimizer" 2>/dev/null; then
  LOGCSV="/opt/xsmom-bot/logs/xsmom-optimizer/history.csv"
  mkdir -p "$(dirname "${LOGCSV}")"
fi

# 1) Optional: ingest recent closed-PnL into CSVs for Phase-2 heuristics
# The optimizer tolerates missing CSVs; this step is best-effort.
PNL_INGEST="/opt/xsmom-bot/bin/pnl_ingest.py"
PNL_GLOB="${REPORTS_DIR}/Bybit-AllPerp-ClosedPNL-*.csv"

if [[ -f "${PNL_INGEST}" ]]; then
  echo "[run-optimizer] Ingesting recent PnL → ${REPORTS_DIR}"
  set +e
  "${PYTHON_BIN}" "${PNL_INGEST}" \
    --config "${CONFIG}" \
    --since-hours 72 \
    --outdir "${REPORTS_DIR}" \
    --debug
  PNL_RC=$?
  set -e
  if [[ ${PNL_RC} -ne 0 ]]; then
    echo "[run-optimizer] WARN: pnl_ingest.py exited with ${PNL_RC}; continuing without PnL CSVs"
  fi
else
  echo "[run-optimizer] NOTE: ${PNL_INGEST} not found; skipping PnL ingestion."
fi

# 2) Run the optimizer (Phase 1 + Phase 2 sweeps)
echo "[run-optimizer] Starting optimizer…"
CMD=( "${PYTHON_BIN}" -m "src.optimizer_runner"
  --config "${CONFIG}"
  --objective "sharpe"
  --min-improve-rel "0.03"
  --require-no-worse-mdd
  --max-turnover-per-year "200"
  --phase2 --phase2-passes "1" --phase2-extra
  --allow-enable
  --pnl-csv-glob "${PNL_GLOB}"
  --blacklist-min-trades "6" --blacklist-top-k "10" --blacklist-lookback-hours "72"
  --tod-sweep --tod-remove-counts "2,6"
  --log-csv "${LOGCSV}"
)

# Allow the caller/service to append flags via OPTIMIZER_EXTRA_FLAGS
if [[ -n "${OPTIMIZER_EXTRA_FLAGS:-}" ]]; then
  echo "[run-optimizer] Extra flags: ${OPTIMIZER_EXTRA_FLAGS}"
  # shellcheck disable=SC2206
  EXTRA=( ${OPTIMIZER_EXTRA_FLAGS} )
  CMD+=( "${EXTRA[@]}" )
fi

echo "[run-optimizer] CMD: ${CMD[*]}"
exec "${CMD[@]}"
