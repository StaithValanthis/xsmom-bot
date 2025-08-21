#!/usr/bin/env bash
set -euo pipefail

# =========================
# Configurable parameters
# =========================
APP_NAME="xsmom-bot"
DEFAULT_APP_DIR="/opt/${APP_NAME}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
RUN_AS="${RUN_AS:-ubuntu}"        # change if you run as another user
RUN_GROUP="${RUN_GROUP:-$RUN_AS}"
SERVICE_NAME="${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
START_NOW="${START_NOW:-ask}"      # ask | yes | no
RUN_NONINTERACTIVE="${RUN_NONINTERACTIVE:-0}"

# 1 = dry-run (default, safe), 0 = live trading
SERVICE_DRY="${SERVICE_DRY:-1}"

# =========================
# Helpers
# =========================
info(){ echo "[*] $*"; }
warn(){ echo "[!] $*" >&2; }
die(){ echo "[x] $*" >&2; exit 1; }

ensure_dir_owned() {
  local d="$1"
  sudo mkdir -p "$d"
  sudo chown -R "${RUN_AS}:${RUN_GROUP}" "$d"
}

seed_local_if_missing() {
  mkdir -p config src systemd state logs tests

  if [ ! -f ".env.example" ]; then
    cat > .env.example <<'EOF'
BYBIT_API_KEY=
BYBIT_API_SECRET=
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
EOF
  fi

  if [ ! -f "requirements.txt" ]; then
    cat > requirements.txt <<'EOF'
ccxt>=4.1,<5
pandas==2.2.2
numpy==1.26.4
tenacity==8.5.0
pydantic==2.8.2
python-dateutil==2.9.0.post0
PyYAML==6.0.2
EOF
  fi

  if [ ! -f "config/config.yaml.example" ]; then
    cat > config/config.yaml.example <<'EOF'
exchange:
  id: bybit
  account_type: swap
  quote: USDT
  only_perps: true
  unified_margin: true
  testnet: false
  max_symbols: 80
  min_usd_volume_24h: 5000000
  min_price: 0.005
  timeframe: "1h"
  candles_limit: 1500
strategy:
  lookbacks: [1, 6, 24]
  lookback_weights: [1.0, 1.0, 1.0]
  vol_lookback: 72
  k_min: 8
  k_max: 24
  market_neutral: true
  gross_leverage: 1.5
  max_weight_per_asset: 0.08
  regime_filter:
    enabled: true
    ema_len: 200
    slope_min_bps_per_day: 4
  funding_tilt:
    enabled: true
    weight: 0.15
liquidity:
  adv_cap_pct: 0.002
  notional_cap_usdt: 40000
execution:
  order_type: limit
  post_only: true
  price_offset_bps: 3
  slippage_bps_guard: 20
  set_leverage: 3
  rebalance_minute: 5
  poll_seconds: 15
risk:
  atr_mult_sl: 2.0
  atr_mult_tp: 3.5
  use_tp: true
  max_daily_loss_pct: 2.5
  trade_disable_minutes: 720
costs:
  taker_fee_bps: 6.0
  maker_fee_bps: 1.0
  slippage_bps: 3.0
  funding_bps_per_day: 0.8
paths:
  state_path: "state/state.json"
  logs_dir: "logs"
logging:
  level: INFO
  file_max_mb: 20
  file_backups: 5
EOF
  fi

  if [ ! -f "systemd/${SERVICE_NAME}.service" ]; then
    cat > "systemd/${SERVICE_NAME}.service" <<'EOF'
[Unit]
Description=Multi-pair XSMOM Crypto Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/xsmom-bot
EnvironmentFile=/opt/xsmom-bot/.env
ExecStart=/opt/xsmom-bot/venv/bin/python -m src.main live --config /opt/xsmom-bot/config/config.yaml --dry
Restart=always
RestartSec=10
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
  fi
}

normalize_destination_tree() {
  local dest="$1"
  sudo mkdir -p "${dest}/config" "${dest}/src" "${dest}/systemd" "${dest}/state" "${dest}/logs"
  sudo chown -R "${RUN_AS}:${RUN_GROUP}" "${dest}"

  if [ -f "${dest}/config.yaml.example" ]; then
    sudo mv "${dest}/config.yaml.example" "${dest}/config/"
  fi
  if [ -f "${dest}/config.yaml" ]; then
    sudo mv "${dest}/config.yaml" "${dest}/config/"
  fi
  if [ -f "${dest}/${SERVICE_NAME}.service" ]; then
    sudo mv "${dest}/${SERVICE_NAME}.service" "${dest}/systemd/"
  fi

  for f in __init__.py backtester.py config.py exchange.py live.py main.py risk.py signals.py utils.py; do
    if [ -f "${dest}/${f}" ]; then
      sudo mv "${dest}/${f}" "${dest}/src/"
    fi
  done
  if [ -f "${dest}/sizing.py" ]; then
    sudo mv "${dest}/sizing.py" "${dest}/src/sizing.py"
  fi
}

ensure_active_config() {
  local dest="$1"
  if [ ! -f "${dest}/config/config.yaml" ]; then
    if [ -f "${dest}/config/config.yaml.example" ]; then
      sudo -u "${RUN_AS}" cp "${dest}/config/config.yaml.example" "${dest}/config/config.yaml"
    else
      warn "config.yaml.example missing at ${dest}/config; seeding a minimal config.yaml"
      sudo -u "${RUN_AS}" tee "${dest}/config/config.yaml" >/dev/null <<'EOF'
exchange: {id: bybit, account_type: swap, quote: USDT, only_perps: true, unified_margin: true, testnet: false,
  max_symbols: 80, min_usd_volume_24h: 5000000, min_price: 0.005, timeframe: "1h", candles_limit: 1500}
strategy: {lookbacks: [1,6,24], lookback_weights: [1,1,1], vol_lookback: 72, k_min: 8, k_max: 24,
  market_neutral: true, gross_leverage: 1.5, max_weight_per_asset: 0.08,
  regime_filter: {enabled: true, ema_len: 200, slope_min_bps_per_day: 4},
  funding_tilt: {enabled: true, weight: 0.15}}
liquidity: {adv_cap_pct: 0.002, notional_cap_usdt: 40000}
execution: {order_type: limit, post_only: true, price_offset_bps: 3, slippage_bps_guard: 20,
  set_leverage: 3, rebalance_minute: 5, poll_seconds: 15}
risk: {atr_mult_sl: 2.0, atr_mult_tp: 3.5, use_tp: true, max_daily_loss_pct: 2.5, trade_disable_minutes: 720}
costs: {taker_fee_bps: 6.0, maker_fee_bps: 1.0, slippage_bps: 3.0, funding_bps_per_day: 0.8}
paths: {state_path: "state/state.json", logs_dir: "logs"}
logging: {level: INFO, file_max_mb: 20, file_backups: 5}
EOF
    fi
  fi
}

ensure_env_file() {
  local dest="$1"
  if [ ! -f "${dest}/.env" ]; then
    if [ -f "${dest}/.env.example" ]; then
      sudo -u "${RUN_AS}" cp "${dest}/.env.example" "${dest}/.env"
      info "-> Edit ${dest}/.env to set BYBIT_API_KEY/SECRET"
    else
      sudo -u "${RUN_AS}" tee "${dest}/.env" >/dev/null <<'EOF'
BYBIT_API_KEY=
BYBIT_API_SECRET=
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
EOF
      info "-> Edit ${dest}/.env to set BYBIT_API_KEY/SECRET"
    fi
  fi
}

fix_service_user_and_paths() {
  local dest="$1"
  local svc="${dest}/systemd/${SERVICE_NAME}.service"

  if [ ! -f "$svc" ]; then
    warn "Service file missing; seeding a minimal one."
    cat > "$svc" <<EOF
[Unit]
Description=Multi-pair XSMOM Crypto Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_AS}
Group=${RUN_GROUP}
WorkingDirectory=${dest}
EnvironmentFile=${dest}/.env
ExecStart=${dest}/venv/bin/python -m src.main live --config ${dest}/config/config.yaml --dry
Restart=always
RestartSec=10
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
  fi

  local EXEC_FLAGS="--config ${dest}/config/config.yaml"
  if [ "${SERVICE_DRY}" = "1" ]; then
    EXEC_FLAGS="${EXEC_FLAGS} --dry"
  fi

  sudo sed -i "s|^User=.*|User=${RUN_AS}|" "$svc"
  sudo sed -i "s|^Group=.*|Group=${RUN_GROUP}|" "$svc" || true
  sudo sed -i "s|^WorkingDirectory=.*|WorkingDirectory=${dest}|" "$svc"
  sudo sed -i "s|^ExecStart=.*|ExecStart=${dest}/venv/bin/python -m src.main live ${EXEC_FLAGS}|" "$svc"
}

# =========================
# Begin installation
# =========================

APP_DIR="$DEFAULT_APP_DIR"
info "Ensuring /opt exists (or fallback to \$HOME)..."
if ! sudo mkdir -p /opt 2>/dev/null; then
  warn "Could not create /opt; falling back to \$HOME/${APP_NAME}-app"
  APP_DIR="${HOME}/${APP_NAME}-app"
  mkdir -p "${APP_DIR}"
else
  sudo chown -R "${RUN_AS}:${RUN_GROUP}" /opt
fi
info "App directory will be: ${APP_DIR}"

info "Seeding local missing files (repo side) so rsync won't fail..."
seed_local_if_missing

info "Creating app directory at ${APP_DIR}..."
ensure_dir_owned "${APP_DIR}"

info "Copying repository files into ${APP_DIR}..."
sudo rsync -a --delete \
  README.md requirements.txt .env.example install.sh run_local.sh \
  config/ src/ systemd/ state/ logs/ tests/ \
  "${APP_DIR}/" || true

info "Normalizing destination tree..."
normalize_destination_tree "${APP_DIR}"

info "Setting permissions..."
sudo chown -R "${RUN_AS}:${RUN_GROUP}" "${APP_DIR}"

info "Creating Python venv and installing requirements..."
sudo -u "${RUN_AS}" "${PYTHON_BIN}" -m venv "${APP_DIR}/venv"
sudo -u "${RUN_AS}" "${APP_DIR}/venv/bin/pip" install --upgrade pip
if grep -q '^tccxt' "${APP_DIR}/requirements.txt"; then
  sudo sed -i 's/^tccxt/ccxt/' "${APP_DIR}/requirements.txt"
fi
sudo -u "${RUN_AS}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

info "Creating .env from example (if missing)..."
ensure_env_file "${APP_DIR}"

info "Creating config.yaml from example (if missing)..."
ensure_active_config "${APP_DIR}"

info "Installing systemd service..."
fix_service_user_and_paths "${APP_DIR}"
sudo cp "${APP_DIR}/systemd/${SERVICE_NAME}.service" "${SERVICE_FILE}"

# Install optimizer service + timer
if [ -f "${APP_DIR}/systemd/xsmom-opt.service" ]; then
  sudo cp "${APP_DIR}/systemd/xsmom-opt.service" "/etc/systemd/system/xsmom-opt.service"
fi
if [ -f "${APP_DIR}/systemd/xsmom-opt.timer" ]; then
  sudo cp "${APP_DIR}/systemd/xsmom-opt.timer" "/etc/systemd/system/xsmom-opt.timer"
fi

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

# Enable optimizer timer if present
if systemctl list-unit-files | grep -q "^xsmom-opt.timer"; then
  sudo systemctl enable xsmom-opt.timer || true
  sudo systemctl start xsmom-opt.timer || true
  info "Optimizer timer enabled (xsmom-opt.timer)."
fi

# Start service now?
if [ "${RUN_NONINTERACTIVE}" = "1" ] || [ "${START_NOW}" = "yes" ]; then
  info "Starting service..."
  sudo systemctl start "${SERVICE_NAME}"
  info "Service started. Tail logs with: journalctl -u ${SERVICE_NAME} -f -o cat"
elif [ "${START_NOW}" = "no" ]; then
  info "Service installed but not started. Start later with: sudo systemctl start ${SERVICE_NAME}"
else
  read -r -p "[?] Start service now? (y/N) " yn || true
  yn="${yn:-N}"
  if [[ "${yn,,}" == "y" ]]; then
    sudo systemctl start "${SERVICE_NAME}"
    info "Service started. Tail logs with: journalctl -u ${SERVICE_NAME} -f -o cat"
  else
    info "You can start later with: sudo systemctl start ${SERVICE_NAME}"
  fi
fi

info "Done."

echo
echo "=== Next steps ==="
echo "1) Edit ${APP_DIR}/.env and set BYBIT_API_KEY / BYBIT_API_SECRET"
echo "2) Edit ${APP_DIR}/config/config.yaml as needed"
if [ "${SERVICE_DRY}" = "1" ]; then
  echo "Service mode: DRY-RUN (no live orders). To switch to LIVE:"
  echo "   sudo systemctl stop ${SERVICE_NAME}"
  echo "   sudo sed -i 's/ --dry\\b//' ${SERVICE_FILE}"
  echo "   sudo systemctl daemon-reload && sudo systemctl start ${SERVICE_NAME}"
else
  echo "Service mode: LIVE TRADING (no --dry). Make sure keys/permissions are correct!"
fi
echo
echo "Manage service:"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "  sudo systemctl stop ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f -o cat"
