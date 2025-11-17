#!/usr/bin/env bash
set -euo pipefail

# =========================
# XSMOM Bot Installer (full stack, one-shot)
# Installs under /opt/xsmom-bot, creates venv, seeds .env/config,
# installs/patches systemd units for:
#   - xsmom-bot.service (main trading bot)
#   - xsmom-optimizer.service + xsmom-optimizer.timer
#   - xsmom-meta-trainer.service + xsmom-meta-trainer.timer
#   - xsmom-daily-report.service + xsmom-daily-report.timer
#   - xsmom-rollout-supervisor.service + xsmom-rollout-supervisor.timer
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
AUTO_INSTALL_PACKAGES="${AUTO_INSTALL_PACKAGES:-1}"  # 1 => auto-install missing packages

# Optimizer timer & GRID defaults
OPTIMER_CALENDAR="${OPTIMER_CALENDAR:-*-*-* 00:30:00}"
OPT_ENV_FILE="/etc/default/xsmom-optimizer"
DEFAULT_GRID='{"k":[2,4,6,8],"gross":[0.8,1.0,1.2]}'

# Meta-trainer timer default
META_CALENDAR="${META_CALENDAR:-01:20}"

info(){ echo -e "\033[1;32m[+]\033[0m $*"; }
warn(){ echo -e "\033[1;33m[!]\033[0m $*"; }
die(){ echo -e "\033[1;31m[x]\033[0m $*"; exit 1; }

# =========================
# Pre-flight checks & package installation
# =========================
check_and_install_packages(){
  info "Checking and installing system packages..."
  
  local missing_packages=()
  
  # Check Python
  if ! command -v "${PYTHON_BIN}" &> /dev/null; then
    missing_packages+=("python3")
  fi
  
  # Check python3-venv
  if ! "${PYTHON_BIN}" -c "import venv" 2>/dev/null; then
    missing_packages+=("python3-venv")
  fi
  
  # Check pip
  if ! "${PYTHON_BIN}" -m pip --version &> /dev/null 2>&1; then
    missing_packages+=("python3-pip")
  fi
  
  # Check git (optional, but nice to have)
  if ! command -v git &> /dev/null; then
    if [ "${AUTO_INSTALL_PACKAGES}" = "1" ]; then
      missing_packages+=("git")
    else
      warn "git not found. You may need it to update the repo."
    fi
  fi
  
  # Check sudo
  if ! command -v sudo &> /dev/null; then
    die "sudo not found. This installer requires sudo."
  fi
  
  # Check systemctl (for systemd)
  if ! command -v systemctl &> /dev/null; then
    if [ "${AUTO_INSTALL_PACKAGES}" = "1" ]; then
      # systemd is usually pre-installed, but check anyway
      warn "systemctl not found. Systemd services won't be installed."
    else
      warn "systemctl not found. Systemd services won't be installed."
    fi
  fi
  
  # Install missing packages if auto-install enabled
  if [ "${#missing_packages[@]}" -gt 0 ] && [ "${AUTO_INSTALL_PACKAGES}" = "1" ]; then
    info "Installing missing packages: ${missing_packages[*]}"
    if command -v apt-get &> /dev/null; then
      sudo apt-get update -qq
      sudo apt-get install -y "${missing_packages[@]}" || die "Failed to install packages"
    elif command -v yum &> /dev/null; then
      sudo yum install -y "${missing_packages[@]}" || die "Failed to install packages"
    else
      warn "No package manager found (apt-get/yum). Please install manually: ${missing_packages[*]}"
    fi
  elif [ "${#missing_packages[@]}" -gt 0 ]; then
    die "Missing required packages: ${missing_packages[*]}. Install them manually or set AUTO_INSTALL_PACKAGES=1"
  fi
  
  info "System requirements OK."
}

check_requirements(){
  check_and_install_packages
}

# =========================
# Directory and file management
# =========================
ensure_dir_owned(){
  local d="$1"
  sudo mkdir -p "$d"
  sudo chown -R "${RUN_AS}:${RUN_GROUP}" "$d"
}

ensure_env_example(){
  local dest="$1"
  if [ ! -f "${dest}/.env.example" ]; then
    cat > "${dest}/.env.example" <<'EOF'
# xsmom-bot Environment Variables
# Copy this to .env and fill in your actual values
# WARNING: Never commit .env to a public repository!

# Bybit API Credentials (REQUIRED for live trading)
# Get from: https://www.bybit.com/user/api-key (production) or https://testnet.bybit.com/ (testnet)
BYBIT_API_KEY=your_api_key_here
BYBIT_API_SECRET=your_api_secret_here

# Discord Webhook URL (OPTIONAL but recommended for notifications)
# Get from: Discord channel -> Integrations -> Webhooks -> Create Webhook
DISCORD_WEBHOOK_URL=

# Python environment
PYTHONUNBUFFERED=1
EOF
    sudo chown "${RUN_AS}:${RUN_GROUP}" "${dest}/.env.example"
    info "Created ${dest}/.env.example template"
  fi
}

prompt_secrets(){
  local dest="$1"
  local env_file="${dest}/.env"
  
  # Check if .env already exists and has values
  if [ -f "${env_file}" ]; then
    local has_key=""
    local has_secret=""
    if grep -q "^BYBIT_API_KEY=" "${env_file}" 2>/dev/null; then
      has_key=$(grep "^BYBIT_API_KEY=" "${env_file}" | cut -d'=' -f2- | tr -d '"' | tr -d "'" | xargs)
    fi
    if grep -q "^BYBIT_API_SECRET=" "${env_file}" 2>/dev/null; then
      has_secret=$(grep "^BYBIT_API_SECRET=" "${env_file}" | cut -d'=' -f2- | tr -d '"' | tr -d "'" | xargs)
    fi
    
    # Skip if already has valid-looking keys (not placeholder)
    if [ -n "${has_key}" ] && [ "${has_key}" != "your_api_key_here" ] && \
       [ -n "${has_secret}" ] && [ "${has_secret}" != "your_api_secret_here" ]; then
      if [ "${RUN_NONINTERACTIVE}" = "0" ]; then
        echo ""
        read -r -p "[?] .env file already exists with API keys. Overwrite? (y/N) " yn || true
        yn="${yn:-N}"
        if [[ ! "${yn,,}" =~ ^y ]]; then
          info "Keeping existing .env file"
          return 0
        fi
      else
        info "Non-interactive mode: keeping existing .env file"
        return 0
      fi
    fi
  fi
  
  # Create .env from example if missing
  if [ ! -f "${env_file}" ]; then
    ensure_env_example "${dest}"
    cp "${dest}/.env.example" "${env_file}"
    sudo chown "${RUN_AS}:${RUN_GROUP}" "${env_file}"
    sudo chmod 600 "${env_file}"
  fi
  
  # Interactive prompts (skip if non-interactive)
  if [ "${RUN_NONINTERACTIVE}" = "0" ]; then
    echo ""
    warn "⚠️  SECRETS CONFIGURATION"
    warn "You will be prompted for API keys and Discord webhook."
    warn "These will be stored in ${env_file} (never commit this file!)"
    echo ""
    
    # Prompt for Bybit API Key (with validation)
    local api_key=""
    while [ -z "${api_key}" ]; do
      read -r -p "[?] Bybit API Key (required): " api_key || true
      if [ -z "${api_key}" ]; then
        warn "API key cannot be empty. Please enter a valid API key."
      fi
    done
    
    # Update .env file
    if grep -q "^BYBIT_API_KEY=" "${env_file}"; then
      sudo sed -i "s|^BYBIT_API_KEY=.*|BYBIT_API_KEY=${api_key}|" "${env_file}"
    else
      echo "BYBIT_API_KEY=${api_key}" | sudo tee -a "${env_file}" > /dev/null
    fi
    
    # Prompt for Bybit API Secret (hidden input, with validation)
    local api_secret=""
    while [ -z "${api_secret}" ]; do
      echo -n "[?] Bybit API Secret (required, hidden): "
      read -rs api_secret || true
      echo ""
      if [ -z "${api_secret}" ]; then
        warn "API secret cannot be empty. Please enter a valid API secret."
      fi
    done
    
    # Update .env file
    if grep -q "^BYBIT_API_SECRET=" "${env_file}"; then
      sudo sed -i "s|^BYBIT_API_SECRET=.*|BYBIT_API_SECRET=${api_secret}|" "${env_file}"
    else
      echo "BYBIT_API_SECRET=${api_secret}" | sudo tee -a "${env_file}" > /dev/null
    fi
    
    # Prompt for Discord Webhook (optional)
    local discord_webhook=""
    read -r -p "[?] Discord Webhook URL (optional, press Enter to skip): " discord_webhook || true
    if [ -n "${discord_webhook}" ]; then
      if grep -q "^DISCORD_WEBHOOK_URL=" "${env_file}"; then
        sudo sed -i "s|^DISCORD_WEBHOOK_URL=.*|DISCORD_WEBHOOK_URL=${discord_webhook}|" "${env_file}"
      else
        echo "DISCORD_WEBHOOK_URL=${discord_webhook}" | sudo tee -a "${env_file}" > /dev/null
      fi
    fi
    
    # Ensure PYTHONUNBUFFERED is set
    if ! grep -q "^PYTHONUNBUFFERED=" "${env_file}"; then
      echo "PYTHONUNBUFFERED=1" | sudo tee -a "${env_file}" > /dev/null
    fi
    
    # Set restrictive permissions
    sudo chown "${RUN_AS}:${RUN_GROUP}" "${env_file}"
    sudo chmod 600 "${env_file}"
    
    echo ""
    info "Secrets written to ${env_file} (mode 600, owner ${RUN_AS})"
    warn "⚠️  WARNING: Never commit .env to a public repository!"
  else
    warn "Non-interactive mode: Skipping secret prompts. Edit ${env_file} manually."
  fi
}

ensure_active_config(){
  local dest="$1"
  if [ ! -f "${dest}/config/config.yaml" ]; then
    if [ -f "${dest}/config/config.yaml.example" ]; then
      cp "${dest}/config/config.yaml.example" "${dest}/config/config.yaml"
      sudo chown "${RUN_AS}:${RUN_GROUP}" "${dest}/config/config.yaml"
      info "Seeded ${dest}/config/config.yaml from example."
    else
      warn "config.yaml.example not found. You'll need to create config/config.yaml manually."
    fi
  else
    info "Config file already exists: ${dest}/config/config.yaml"
  fi
}

normalize_destination_tree(){
  local dest="$1"
  ensure_dir_owned "${dest}/logs"
  ensure_dir_owned "${dest}/state"
  ensure_dir_owned "${dest}/data"              # For rollout state
  ensure_dir_owned "${dest}/config/optimized"  # For optimized configs
  ensure_dir_owned "${dest}/reports"           # For optimizer reports
}

seed_local_if_missing(){
  # Ensure repo has minimal files so rsync won't fail if user copied partial tree
  mkdir -p config src systemd state logs tests bin data config/optimized reports || true
  touch logs/.gitkeep state/.gitkeep data/.gitkeep config/optimized/.gitkeep reports/.gitkeep || true
}

# =========================
# Systemd service installation
# =========================
fix_service_user_and_paths(){
  local dest="$1"
  # Service files use absolute paths, so we just need to ensure they exist
  # and are copied correctly
  :
}

install_service_unit(){
  local dest="$1"
  local service_name="$2"
  local service_file="${dest}/systemd/${service_name}.service"
  
  if [ -f "${service_file}" ]; then
    # Replace User/Group in service file if needed
    local temp_file=$(mktemp)
    sed "s|User=.*|User=${RUN_AS}|g; s|Group=.*|Group=${RUN_GROUP}|g" "${service_file}" > "${temp_file}"
    sudo cp "${temp_file}" "/etc/systemd/system/${service_name}.service"
    rm -f "${temp_file}"
    info "Installed ${service_name}.service"
  else
    warn "Service file not found: ${service_file}"
    return 1
  fi
}

install_timer_unit(){
  local dest="$1"
  local timer_name="$2"
  local timer_file="${dest}/systemd/${timer_name}.timer"
  
  if [ -f "${timer_file}" ]; then
    sudo cp "${timer_file}" "/etc/systemd/system/${timer_name}.timer"
    info "Installed ${timer_name}.timer"
  else
    warn "Timer file not found: ${timer_file}"
    return 1
  fi
}

install_optimizer_units(){
  local dest="$1"
  install_service_unit "${dest}" "xsmom-optimizer"
  install_timer_unit "${dest}" "xsmom-optimizer"
}

install_meta_trainer_units(){
  local dest="$1"
  install_service_unit "${dest}" "xsmom-meta-trainer"
  install_timer_unit "${dest}" "xsmom-meta-trainer"
}

install_daily_report_units(){
  local dest="$1"
  install_service_unit "${dest}" "xsmom-daily-report"
  install_timer_unit "${dest}" "xsmom-daily-report"
}

install_rollout_supervisor_units(){
  local dest="$1"
  install_service_unit "${dest}" "xsmom-rollout-supervisor"
  install_timer_unit "${dest}" "xsmom-rollout-supervisor"
}

# =========================
# Validation and smoke test
# =========================
validate_installation(){
  local dest="$1"
  
  info "Validating installation..."
  
  # Check venv exists and has Python
  if [ ! -f "${dest}/venv/bin/python" ]; then
    die "Virtual environment not properly created at ${dest}/venv"
  fi
  
  # Check config exists
  if [ ! -f "${dest}/config/config.yaml" ]; then
    warn "config/config.yaml not found. Bot may not run without it."
  fi
  
  # Check .env exists
  if [ ! -f "${dest}/.env" ]; then
    warn ".env file not found. Bot will fail without API keys."
  else
    # Validate .env has required keys (even if placeholder)
    local has_key=false
    local has_secret=false
    if grep -q "^BYBIT_API_KEY=" "${dest}/.env" 2>/dev/null; then
      local key_val=$(grep "^BYBIT_API_KEY=" "${dest}/.env" | cut -d'=' -f2- | tr -d '"' | tr -d "'" | xargs)
      if [ -n "${key_val}" ] && [ "${key_val}" != "your_api_key_here" ]; then
        has_key=true
      fi
    fi
    if grep -q "^BYBIT_API_SECRET=" "${dest}/.env" 2>/dev/null; then
      local secret_val=$(grep "^BYBIT_API_SECRET=" "${dest}/.env" | cut -d'=' -f2- | tr -d '"' | tr -d "'" | xargs)
      if [ -n "${secret_val}" ] && [ "${secret_val}" != "your_api_secret_here" ]; then
        has_secret=true
      fi
    fi
    
    if [ "${has_key}" = "true" ] && [ "${has_secret}" = "true" ]; then
      info "✓ .env file has valid API keys"
    else
      warn ".env file missing or has placeholder API keys. Bot will not work until keys are set."
    fi
  fi
  
  # Try to import main modules (smoke test)
  info "Running smoke test (checking Python imports)..."
  if sudo -u "${RUN_AS}" "${dest}/venv/bin/python" -c "
import sys
sys.path.insert(0, '${dest}')
try:
    from src.config import load_config
    from src.exchange import ExchangeWrapper
    print('✓ Core modules import successfully')
except Exception as e:
    print(f'✗ Import failed: {e}')
    sys.exit(1)
" 2>&1; then
    info "Smoke test passed: Core modules import successfully"
  else
    warn "Smoke test failed: Some modules may not import correctly. Check Python dependencies."
  fi
  
  # Try to load config if it exists
  if [ -f "${dest}/config/config.yaml" ]; then
    info "Testing config loading..."
    if sudo -u "${RUN_AS}" "${dest}/venv/bin/python" -c "
import sys
sys.path.insert(0, '${dest}')
try:
    from src.config import load_config
    cfg = load_config('${dest}/config/config.yaml')
    print(f'✓ Config loaded successfully (universe: {cfg.exchange.max_symbols} symbols)')
except Exception as e:
    print(f'✗ Config load failed: {e}')
    sys.exit(1)
" 2>&1; then
      info "Config validation passed"
    else
      warn "Config validation failed. Check config/config.yaml for errors."
    fi
  fi
  
  # Test CLI help
  info "Testing CLI..."
  if sudo -u "${RUN_AS}" "${dest}/venv/bin/python" -m src.main --help &>/dev/null; then
    info "✓ CLI works (--help succeeds)"
  else
    warn "CLI test failed. Check Python installation."
  fi
}

# =========================
# Main installation flow
# =========================
main(){
  # Pre-flight checks
  check_requirements
  
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
  
  # Seed local repo structure if needed
  info "Seeding local missing files (repo side) so rsync won't fail..."
  seed_local_if_missing
  
  # Create app directory
  info "Creating app directory at ${APP_DIR}..."
  ensure_dir_owned "${APP_DIR}"
  
  # Copy repository files
  info "Copying repository files into ${APP_DIR}..."
  # Ensure .env.example exists before copying
  ensure_env_example "."
  sudo rsync -a --delete \
    README.md requirements.txt .env.example install.sh run_local.sh \
    config/ src/ systemd/ state/ logs/ tests/ bin/ docs/ tools/ \
    "${APP_DIR}/" 2>/dev/null || true
  
  # Normalize destination tree (create required dirs)
  info "Normalizing destination tree..."
  normalize_destination_tree "${APP_DIR}"
  
  # Set permissions
  info "Setting permissions..."
  sudo chown -R "${RUN_AS}:${RUN_GROUP}" "${APP_DIR}"
  
  # Create virtualenv (idempotent: reuse if exists)
  info "Creating Python venv and installing requirements..."
  if [ ! -d "${APP_DIR}/venv" ]; then
    sudo -u "${RUN_AS}" "${PYTHON_BIN}" -m venv "${APP_DIR}/venv"
    info "Created new virtual environment"
  else
    info "Venv already exists, reusing..."
  fi
  
  sudo -u "${RUN_AS}" "${APP_DIR}/venv/bin/pip" install --upgrade pip wheel --quiet
  # Historical typo guard
  if grep -q '^tccxt' "${APP_DIR}/requirements.txt" 2>/dev/null; then
    sudo sed -i 's/^tccxt/ccxt/' "${APP_DIR}/requirements.txt"
  fi
  info "Installing Python dependencies (this may take a few minutes)..."
  sudo -u "${RUN_AS}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt" --quiet
  
  # Create config from example if missing
  info "Creating config.yaml from example (if missing)..."
  ensure_active_config "${APP_DIR}"
  
  # Prompt for and write secrets
  info "Configuring secrets..."
  prompt_secrets "${APP_DIR}"
  
  # Install systemd services
  if command -v systemctl &> /dev/null; then
    info "Installing systemd services..."
    fix_service_user_and_paths "${APP_DIR}"
    
    # Main bot service
    info "Installing bot service..."
    install_service_unit "${APP_DIR}" "${SERVICE_NAME}"
    
    # Optimizer service + timer
    info "Installing optimizer service + timer..."
    install_optimizer_units "${APP_DIR}"
    
    # Meta-trainer service + timer
    info "Installing meta-trainer service + timer..."
    install_meta_trainer_units "${APP_DIR}"
    
    # Daily report service + timer
    info "Installing daily report service + timer..."
    install_daily_report_units "${APP_DIR}"
    
    # Rollout supervisor service + timer
    info "Installing rollout supervisor service + timer..."
    install_rollout_supervisor_units "${APP_DIR}"
    
    # Reload systemd
    sudo systemctl daemon-reload
    
    # Enable services
    info "Enabling services..."
    sudo systemctl enable "${SERVICE_NAME}" 2>/dev/null || warn "Failed to enable ${SERVICE_NAME}"
    
    # Enable timers
    sudo systemctl enable xsmom-optimizer.timer 2>/dev/null || warn "Failed to enable xsmom-optimizer.timer"
    sudo systemctl enable xsmom-meta-trainer.timer 2>/dev/null || warn "Failed to enable xsmom-meta-trainer.timer"
    sudo systemctl enable xsmom-daily-report.timer 2>/dev/null || warn "Failed to enable xsmom-daily-report.timer"
    sudo systemctl enable xsmom-rollout-supervisor.timer 2>/dev/null || warn "Failed to enable xsmom-rollout-supervisor.timer"
    
    # Start timers (services run on schedule)
    sudo systemctl start xsmom-optimizer.timer 2>/dev/null || warn "Failed to start xsmom-optimizer.timer"
    sudo systemctl start xsmom-meta-trainer.timer 2>/dev/null || warn "Failed to start xsmom-meta-trainer.timer"
    sudo systemctl start xsmom-daily-report.timer 2>/dev/null || warn "Failed to start xsmom-daily-report.timer"
    sudo systemctl start xsmom-rollout-supervisor.timer 2>/dev/null || warn "Failed to start xsmom-rollout-supervisor.timer"
    
    info "Systemd services installed and enabled"
  else
    warn "systemctl not found. Skipping systemd service installation."
  fi
  
  # Validate installation
  validate_installation "${APP_DIR}"
  
  # Start service now?
  if command -v systemctl &> /dev/null; then
    if [ "${RUN_NONINTERACTIVE}" = "1" ] || [ "${START_NOW}" = "yes" ]; then
      info "Starting service..."
      sudo systemctl start "${SERVICE_NAME}" 2>/dev/null || warn "Failed to start ${SERVICE_NAME}"
      info "Service started. Tail logs: journalctl -u ${SERVICE_NAME} -f -o cat"
    elif [ "${START_NOW}" = "no" ]; then
      info "Service installed but not started. Start later with: sudo systemctl start ${SERVICE_NAME}"
    else
      echo ""
      read -r -p "[?] Start service now? (y/N) " yn || true
      yn="${yn:-N}"
      if [[ "${yn,,}" =~ ^y ]]; then
        sudo systemctl start "${SERVICE_NAME}" 2>/dev/null || warn "Failed to start ${SERVICE_NAME}"
        info "Service started. Tail logs: journalctl -u ${SERVICE_NAME} -f -o cat"
      else
        info "You can start later with: sudo systemctl start ${SERVICE_NAME}"
      fi
    fi
  fi
  
  echo ""
  info "=== Installation Complete ==="
  info "Installation directory: ${APP_DIR}"
  info "Config file: ${APP_DIR}/config/config.yaml"
  info "Secrets file: ${APP_DIR}/.env (mode 600, owner ${RUN_AS})"
  warn "⚠️  Remember: Never commit .env to a public repository!"
  echo ""
  info "Installed services:"
  echo "  - ${SERVICE_NAME}.service (main trading bot)"
  echo "  - xsmom-optimizer.service + timer (nightly optimization)"
  echo "  - xsmom-meta-trainer.service + timer (daily meta-label training)"
  echo "  - xsmom-daily-report.service + timer (daily performance reports)"
  echo "  - xsmom-rollout-supervisor.service + timer (staging/promotion lifecycle)"
  echo ""
  info "Next steps:"
  echo "  1. Review config: ${APP_DIR}/config/config.yaml"
  echo "  2. Verify secrets: ${APP_DIR}/.env"
  echo "  3. Test on testnet first (set exchange.testnet: true in config)"
  echo "  4. Start bot: sudo systemctl start ${SERVICE_NAME}"
  echo "  5. Monitor logs: journalctl -u ${SERVICE_NAME} -f"
  echo "  6. Check timers: systemctl list-timers | grep xsmom"
  echo ""
  info "For detailed documentation, see: ${APP_DIR}/docs/overview/quickstart.md"
}

# Run main
main
