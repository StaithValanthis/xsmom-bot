#!/usr/bin/env bash
set -euo pipefail

# =========================
# XSMOM Bot Installer (full stack)
# Installs under /opt/xsmom-bot, creates venv, seeds .env/config,
# installs/patches systemd units for:
#   - xsmom-bot.service
#   - xsmom-optimizer.service + xsmom-optimizer.timer
#   - xsmom-meta-trainer.service + xsmom-meta-trainer.timer
# Idempotent: safe to re-run.
# =========================

APP_NAME="xsmom-bot"
SERVICE_NAME="xsmom-bot"
DEFAULT_APP_DIR="/opt/xsmom-bot"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_AS="${RUN_AS:-ubuntu}"
RUN_GROUP="${RUN_GROUP:-${RUN_AS}}"
START_NOW="${START_NOW:-ask}"           # yes|no|ask
SERVICE_DRY="${SERVICE_DRY:-1}"         # 1 => --dry on bot service
RUN_NONINTERACTIVE="${RUN_NONINTERACTIVE:-0}"

# Optimizer timer & GRID defaults
OPTIMER_CALENDAR="${OPTIMER_CALENDAR:-*-*-* 00:30:00}"
OPT_ENV_FILE="/etc/default/xsmom-optimizer"
DEFAULT_GRID='{"k":[2,4,6,8],"gross":[0.8,1.0,1.2]}'

# Meta-trainer timer default
META_CALENDAR="${META_CALENDAR:-01:20}"

info(){ echo -e "\033[1;32m[+]\033[0m $*"; }
warn(){ echo -e "\033[1;33m[!]\033[0m $*"; }
die(){ echo -e "\033[1;31m[x]\033[0m $*"; exit 1; }

ensure_dir_owned(){
  local d="$1"
  sudo mkdir -p "$d"
  sudo chown -R "${RUN_AS}:${RUN_GROUP}" "$d"
}

ensure_env_file(){
  local dest="$1"
  if [ ! -f "${dest}/.env" ]; then
    cp "${dest}/.env.example" "${dest}/.env" || true
    warn "Seeded ${dest}/.env (edit BYBIT_API_* before going live)."
  fi
}

ensure_active_config(){
  local dest="$1"
  if [ ! -f "${dest}/config/config.yaml" ]; then
    cp "${dest}/config/config.yaml.example" "${dest}/config/config.yaml" || true
    warn "Seeded ${dest}/config/config.yaml from example."
  fi
}

normalize_destination_tree(){
  local dest="$1"
  ensure_dir_owned "${dest}/logs"
  ensure_dir_owned "${dest}/state"
}

seed_local_if_missing(){
  # Ensure repo has minimal files so rsync won't fail if user copied partial tree
  mkdir -p config src systemd state logs tests bin || true
  touch logs/.gitkeep state/.gitkeep || true
}

fix_service_user_and_paths(){
  local dest="$1"
  # No content rewrite needed here because units reference absolute /opt paths;
  # ensure they're present under ${dest}.
  :
}

install_optimizer_units(){
  local dest="$1"
  local svc_repo="${dest}/systemd/xsmom-optimizer.service"
  local tim_repo="${dest}/systemd/xsmom-optimizer.timer"

  if [ ! -f "${svc_repo}" ]; then
    cat > /tmp/xsmom-optimizer.service <<EOF
[Unit]
Description=XSMOM optimizer (autotune via backtests)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=${RUN_AS}
Group=${RUN_GROUP}
WorkingDirectory=${dest}
EnvironmentFile=${dest}/.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=${dest}
Environment=PATH=${dest}/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
ExecStart=/bin/bash -lc '${dest}/bin/run-optimizer.sh ${dest}/config/config.yaml ${dest}/reports'
StandardOutput=journal
StandardError=journal

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
Description=Nightly XSMOM optimizer run

[Timer]
OnCalendar=${OPTIMER_CALENDAR}
RandomizedDelaySec=1200
Persistent=true
Unit=xsmom-optimizer.service

[Install]
WantedBy=timers.target
EOF
    sudo mv /tmp/xsmom-optimizer.timer /etc/systemd/system/xsmom-optimizer.timer
  else
    sudo cp "${tim_repo}" "/etc/systemd/system/xsmom-optimizer.timer"
  fi
}

install_meta_trainer_units(){
  local dest="$1"
  local svc_repo="${dest}/systemd/xsmom-meta-trainer.service"
  local tim_repo="${dest}/systemd/xsmom-meta-trainer.timer"

  if [ ! -f "${svc_repo}" ]; then
    cat > /tmp/xsmom-meta-trainer.service <<EOF
[Unit]
Description=XSMOM meta-label trainer (EOD)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${RUN_AS}
Group=${RUN_GROUP}
WorkingDirectory=${dest}
Environment=PYTHONPATH=${dest}
ExecStart=${dest}/venv/bin/python3 -m src.meta_label_trainer --config ${dest}/config/config.yaml
StandardOutput=append:/var/log/xsmom-meta-trainer.log
StandardError=append:/var/log/xsmom-meta-trainer.log
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    sudo mv /tmp/xsmom-meta-trainer.service /etc/systemd/system/xsmom-meta-trainer.service
  else
    sudo cp "${svc_repo}" "/etc/systemd/system/xsmom-meta-trainer.service"
  fi

  if [ ! -f "${tim_repo}" ]; then
    cat > /tmp/xsmom-meta-trainer.timer <<EOF
[Unit]
Description=Run XSMOM meta-label trainer once per day

[Timer]
OnCalendar=${META_CALENDAR}
Persistent=true
RandomizedDelaySec=900
Unit=xsmom-meta-trainer.service

[Install]
WantedBy=timers.target
EOF
    sudo mv /tmp/xsmom-meta-trainer.timer /etc/systemd/system/xsmom-meta-trainer.timer
  else
    sudo cp "${tim_repo}" "/etc/systemd/system/xsmom-meta-trainer.timer"
  fi

  sudo touch /var/log/xsmom-meta-trainer.log
  sudo chown "${RUN_AS}:${RUN_GROUP}" /var/log/xsmom-meta-trainer.log
}

# =========================
# Begin installation
# =========================
APP_DIR="$DEFAULT_APP_DIR"
info "Ensuring /opt exists (or fallback to $HOME)..."
if ! sudo mkdir -p /opt 2>/dev/null; then
  warn "Could not create /opt; falling back to $HOME/${APP_NAME}-app"
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
sudo rsync -a --delete   README.md requirements.txt .env.example install.sh run_local.sh   config/ src/ systemd/ state/ logs/ tests/ bin/   "${APP_DIR}/" || true

info "Normalizing destination tree..."
normalize_destination_tree "${APP_DIR}"

info "Setting permissions..."
sudo chown -R "${RUN_AS}:${RUN_GROUP}" "${APP_DIR}"

info "Creating Python venv and installing requirements..."
sudo -u "${RUN_AS}" "${PYTHON_BIN}" -m venv "${APP_DIR}/venv"
sudo -u "${RUN_AS}" "${APP_DIR}/venv/bin/pip" install --upgrade pip wheel
# historical typo guard
if grep -q '^tccxt' "${APP_DIR}/requirements.txt" 2>/dev/null; then
  sudo sed -i 's/^tccxt/ccxt/' "${APP_DIR}/requirements.txt"
fi
sudo -u "${RUN_AS}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

info "Creating .env from example (if missing)..."
ensure_env_file "${APP_DIR}"

info "Creating config.yaml from example (if missing)..."
ensure_active_config "${APP_DIR}"

info "Installing bot service..."
fix_service_user_and_paths "${APP_DIR}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
sudo cp "${APP_DIR}/systemd/${SERVICE_NAME}.service" "${SERVICE_FILE}"

info "Installing optimizer service + timer (canonical: xsmom-optimizer.*)..."
install_optimizer_units "${APP_DIR}"

info "Installing meta-trainer service + timer..."
install_meta_trainer_units "${APP_DIR}"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

# Enable timers
sudo systemctl enable xsmom-optimizer.timer || true
sudo systemctl start  xsmom-optimizer.timer || true
sudo systemctl enable xsmom-meta-trainer.timer || true
sudo systemctl start  xsmom-meta-trainer.timer || true

info "Timers enabled: xsmom-optimizer.timer, xsmom-meta-trainer.timer"

# Start service now?
if [ "${RUN_NONINTERACTIVE}" = "1" ] || [ "${START_NOW}" = "yes" ]; then
  info "Starting service..."
  sudo systemctl start "${SERVICE_NAME}"
  info "Service started. Tail logs: journalctl -u ${SERVICE_NAME} -f -o cat"
elif [ "${START_NOW}" = "no" ]; then
  info "Service installed but not started. Start later with: sudo systemctl start ${SERVICE_NAME}"
else
  read -r -p "[?] Start service now? (y/N) " yn || true
  yn="${yn:-N}"
  if [[ "${yn,,}" == "y" ]]; then
    sudo systemctl start "${SERVICE_NAME}"
    info "Service started. Tail logs: journalctl -u ${SERVICE_NAME} -f -o cat"
  else
    info "You can start later with: sudo systemctl start ${SERVICE_NAME}"
  fi
fi

info "Done."
