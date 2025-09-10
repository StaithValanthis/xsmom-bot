#!/usr/bin/env bash
set -euo pipefail

# =========================
# XSMOM Bot Installer
# Installs the app under /opt/xsmom-bot (default), creates venv, seeds config,
# installs/patches systemd units for:
#   - xsmom-bot.service
#   - xsmom-optimizer.service (+ timer)
# Idempotent: safe to re-run after pulling updates.
# =========================

APP_NAME="xsmom-bot"
SERVICE_NAME="xsmom-bot"
DEFAULT_APP_DIR="/opt/xsmom-bot"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_AS="${RUN_AS:-ubuntu}"
RUN_GROUP="${RUN_GROUP:-${RUN_AS}}"
START_NOW="${START_NOW:-ask}"           # yes|no|ask
SERVICE_DRY="${SERVICE_DRY:-1}"         # 1 => --dry on bot service
OPTIMER_CALENDAR="${OPTIMER_CALENDAR:-*-*-* 00:30:00}"
OPT_ENV_FILE="/etc/default/xsmom-optimizer"

info(){ echo -e "\033[1;32m[+]\033[0m $*"; }
warn(){ echo -e "\033[1;33m[!]\033[0m $*"; }
die(){ echo -e "\033[1;31m[x]\033[0m $*"; exit 1; }

ensure_dir_owned(){
  local d="$1"
  sudo mkdir -p "$d"
  sudo chown -R "${RUN_AS}:${RUN_GROUP}" "$d"
}

seed_local_if_missing(){
  # Ensure minimal tree exists in repo (for rsync convenience)
  mkdir -p config logs state systemd src tests || true
  touch logs/.gitkeep state/.gitkeep || true
  # If there is no example config, stub one
  if [ ! -f config/config.yaml.example ]; then
    cat > config/config.yaml.example <<'YAML'
exchange:
  id: bybit
  account_type: swap
  quote: USDT
  only_perps: true
  unified_margin: true
  testnet: false
  max_symbols: 30
  min_usd_volume_24h: 20000000
  min_price: 0.03
  timeframe: 1h
  candles_limit: 1500
strategy:
  ensemble:
    enabled: true
    weights: {xsec: 0.6, ts: 0.2, breakout: 0.2}
    ts_len: 48
    breakout_len: 96
  funding_trim:
    enabled: true
    threshold_bps: 1.8
    slope_per_bps: 0.15
    max_reduction: 0.5
  confirmation:
    enabled: true
    lookback_bars: 2
    z_boost: 0.0
  signal_power: 1.35
  lookbacks: [12,24,48,96]
  lookback_weights: [0.4,0.3,0.2,0.1]
  vol_lookback: 96
  k_min: 2
  k_max: 8
  market_neutral: true
  gross_leverage: 1.8
  max_weight_per_asset: 0.1
  entry_zscore_min: 0.45
  regime_filter: {enabled: true, ema_len: 200, slope_min_bps_per_day: 2.0, use_abs: false}
  adx_filter:
    enabled: true
    len: 14
    min_adx: 20.0
    require_rising: false
    use_di_alignment: true
    min_di_separation: 0.0
    di_hysteresis_bps: 0.0
  symbol_filter:
    enabled: true
    whitelist: []
    banlist: []
    score:
      enabled: true
      min_sample_trades: 8
      ema_alpha: 0.2
      min_win_rate_pct: 38.0
      min_pf: 1.05
      min_pnl_usd: -250.0
      decay_bps_per_day: 3.0
      ban_duration_days: 5
      downweight_floor: 0.25
risk:
  max_drawdown_pct: 25.0
  dd_stepdown:
    enabled: true
    thresholds: [5, 10, 15, 20]
    scalers:   [0.9, 0.8, 0.6, 0.4]
  vol_target:
    enabled: true
    annual_vol_pct: 18.0
logging:
  level: INFO
paths:
  state_path: /opt/xsmom-bot/state
  logs_dir: /opt/xsmom-bot/logs
YAML
  fi
}

normalize_destination_tree(){
  local dest="$1"
  sudo mkdir -p "${dest}/config" "${dest}/logs" "${dest}/state" "${dest}/systemd"
  sudo chown -R "${RUN_AS}:${RUN_GROUP}" "${dest}"
}

ensure_env_file(){
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

ensure_active_config(){
  local dest="$1"
  if [ ! -f "${dest}/config/config.yaml" ]; then
    if [ -f "${dest}/config/config.yaml.example" ]; then
      sudo -u "${RUN_AS}" cp "${dest}/config/config.yaml.example" "${dest}/config/config.yaml"
    else
      die "Missing ${dest}/config/config.yaml(.example)."
    fi
  fi
}

fix_service_user_and_paths(){
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

install_optimizer_units(){
  local dest="$1"

  # Environment defaults for the optimizer grid
  if [ ! -f "${OPT_ENV_FILE}" ]; then
    sudo tee "${OPT_ENV_FILE}" >/dev/null <<'ENV'
# JSON grid for optimizer_cli (quote the value if editing in shell)
GRID={"k":[2,4,6,8],"gross":[0.8,1.0,1.2]}
# Optional: override optimizer OnCalendar (systemd timer) via OPTIMER_CALENDAR env to install.sh
ENV
    sudo chown root:root "${OPT_ENV_FILE}"
    sudo chmod 0644 "${OPT_ENV_FILE}"
  fi

  # Prefer repo-provided units if they exist; otherwise, seed robust defaults.
  local svc_repo="${dest}/systemd/xsmom-optimizer.service"
  local tim_repo="${dest}/systemd/xsmom-optimizer.timer"

  if [ ! -f "${svc_repo}" ]; then
    cat > /tmp/xsmom-optimizer.service <<EOF
[Unit]
Description=XSMOM daily optimizer (purged-CV walk-forward)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${RUN_AS}
Group=${RUN_GROUP}
WorkingDirectory=${dest}
EnvironmentFile=-${OPT_ENV_FILE}
# Sanitise GRID for python -m src.optimizer_cli
ExecStart=${dest}/venv/bin/python -m src.optimizer_cli --config ${dest}/config/config.yaml --objective sharpe --splits 3 --embargo 0.02 --max-symbols 20 --grid "\$GRID"
StandardOutput=append:/var/log/xsmom-optimizer.log
StandardError=append:/var/log/xsmom-optimizer.log
Nice=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
    sudo mv /tmp/xsmom-optimizer.service /etc/systemd/system/xsmom-optimizer.service
  else
    sudo cp "${svc_repo}" "/etc/systemd/system/xsmom-optimizer.service"
  fi

  if [ ! -f "${tim_repo}" ]; then
    cat > /tmp/xsmom-optimizer.timer <<EOF
[Unit]
Description=Run XSMOM optimizer daily and at boot

[Timer]
OnBootSec=3min
OnCalendar=${OPTIMER_CALENDAR}
Persistent=true
Unit=xsmom-optimizer.service

[Install]
WantedBy=timers.target
EOF
    sudo mv /tmp/xsmom-optimizer.timer /etc/systemd/system/xsmom-optimizer.timer
  else
    sudo cp "${tim_repo}" "/etc/systemd/system/xsmom-optimizer.timer"
  fi

  # Back-compat: if older xsmom-opt.* units ship in repo, install them too.
  if [ -f "${dest}/systemd/xsmom-opt.service" ]; then
    sudo cp "${dest}/systemd/xsmom-opt.service" "/etc/systemd/system/xsmom-opt.service"
  fi
  if [ -f "${dest}/systemd/xsmom-opt.timer" ]; then
    sudo cp "${dest}/systemd/xsmom-opt.timer" "/etc/systemd/system/xsmom-opt.timer"
  fi

  # Ensure log file exists and is writable by the service user
  sudo touch /var/log/xsmom-optimizer.log
  sudo chown "${RUN_AS}:${RUN_GROUP}" /var/log/xsmom-optimizer.log
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
sudo -u "${RUN_AS}" "${APP_DIR}/venv/bin/pip" install --upgrade pip wheel
# A few repos mis-typed 'tccxt' historically; auto-fix
if grep -q '^tccxt' "${APP_DIR}/requirements.txt" 2>/dev/null; then
  sudo sed -i 's/^tccxt/ccxt/' "${APP_DIR}/requirements.txt"
fi
sudo -u "${RUN_AS}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

info "Creating .env from example (if missing)..."
ensure_env_file "${APP_DIR}"

info "Creating config.yaml from example (if missing)..."
ensure_active_config "${APP_DIR}"

info "Installing systemd service..."
fix_service_user_and_paths "${APP_DIR}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
sudo cp "${APP_DIR}/systemd/${SERVICE_NAME}.service" "${SERVICE_FILE}"

info "Installing optimizer service + timer..."
install_optimizer_units "${APP_DIR}"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

# Enable optimizer timer
sudo systemctl enable xsmom-optimizer.timer || true
sudo systemctl start xsmom-optimizer.timer || true
info "Optimizer timer enabled (xsmom-optimizer.timer)."

# Start service now?
if [ "${START_NOW}" = "yes" ]; then
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
echo
echo "Optimizer logs: tail -f /var/log/xsmom-optimizer.log"
