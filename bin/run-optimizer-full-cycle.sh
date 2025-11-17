#!/usr/bin/env bash
# run-optimizer-full-cycle.sh — full-cycle optimizer (WFO + BO + MC)
# Usage: run-optimizer-full-cycle.sh [--deploy]
# Runs full-cycle optimizer and optionally deploys new config.
set -euo pipefail

# Configuration
CONFIG="${1:-/opt/xsmom-bot/config/config.yaml}"
DEPLOY="${DEPLOY:-false}"  # Set to "true" to enable auto-deployment

# venv python (override via env if needed)
export PYTHON_BIN="${PYTHON_BIN:-/opt/xsmom-bot/venv/bin/python}"
export PYTHONPATH="${PYTHONPATH:-/opt/xsmom-bot}"

# Load .env (Bybit API keys, etc.) if present
if [[ -f "/opt/xsmom-bot/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "/opt/xsmom-bot/.env"; set +a
fi

echo "[run-optimizer-full-cycle] Using interpreter: ${PYTHON_BIN}"
echo "[run-optimizer-full-cycle] Using CONFIG: ${CONFIG}"
echo "[run-optimizer-full-cycle] Deploy: ${DEPLOY}"

# Create log directory
mkdir -p "/opt/xsmom-bot/logs"

# Parse command-line arguments
DEPLOY_FLAG=""
if [[ "${1:-}" == "--deploy" ]] || [[ "${DEPLOY}" == "true" ]]; then
  DEPLOY_FLAG="--deploy"
  echo "[run-optimizer-full-cycle] AUTO-DEPLOYMENT ENABLED"
fi

# Run full-cycle optimizer
CMD=(
  "${PYTHON_BIN}" -m "src.optimizer.full_cycle"
  --base-config "${CONFIG}"
  --live-config "${CONFIG}"
  --train-days "${TRAIN_DAYS:-120}"
  --oos-days "${OOS_DAYS:-30}"
  --embargo-days "${EMBARGO_DAYS:-2}"
  --bo-evals "${BO_EVALS:-100}"
  --bo-startup "${BO_STARTUP:-10}"
  --mc-runs "${MC_RUNS:-1000}"
  --min-improve-sharpe "${MIN_IMPROVE_SHARPE:-0.05}"
  --min-improve-ann "${MIN_IMPROVE_ANN:-0.03}"
  --max-dd-increase "${MAX_DD_INCREASE:-0.05}"
  --tail-dd-limit "${TAIL_DD_LIMIT:-0.70}"
  --seed "${SEED:-42}"
  --output "/opt/xsmom-bot/logs/optimizer_full_cycle_$(date +%Y%m%d_%H%M%S).json"
)

if [[ -n "${DEPLOY_FLAG}" ]]; then
  CMD+=( "${DEPLOY_FLAG}" )
fi

# Allow caller/service to append flags via OPTIMIZER_EXTRA_FLAGS
if [[ -n "${OPTIMIZER_EXTRA_FLAGS:-}" ]]; then
  echo "[run-optimizer-full-cycle] Extra flags: ${OPTIMIZER_EXTRA_FLAGS}"
  # shellcheck disable=SC2206
  EXTRA=( ${OPTIMIZER_EXTRA_FLAGS} )
  CMD+=( "${EXTRA[@]}" )
fi

echo "[run-optimizer-full-cycle] CMD: ${CMD[*]}"
exec "${CMD[@]}"

