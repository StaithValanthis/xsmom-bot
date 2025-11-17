# Install Script Test Plan

## Overview

This document provides a test plan for validating the `install.sh` script works correctly on a fresh machine.

---

## Test Environment Setup

### Prerequisites

1. **Fresh Ubuntu/Debian machine** (20.04+ or Debian 11+)
   - Minimal install or clean VM
   - Root/sudo access

2. **Required system packages** (install if missing):
   ```bash
   sudo apt-get update
   sudo apt-get install -y python3 python3-pip python3-venv git rsync sudo
   ```

3. **Test user** (for running the bot):
   ```bash
   sudo useradd -m -s /bin/bash ubuntu  # or use existing ubuntu user
   ```

---

## Test Case 1: Fresh Installation (Interactive Mode)

### Steps

1. **Clone or copy repo:**
   ```bash
   git clone <repo-url> xsmom-bot
   cd xsmom-bot
   # OR copy repo files to test directory
   ```

2. **Make install script executable:**
   ```bash
   chmod +x install.sh
   ```

3. **Run installer (interactive):**
   ```bash
   ./install.sh
   ```

4. **During installation:**
   - When prompted for **Bybit API Key**: Enter test key (e.g., `test_api_key_123`)
   - When prompted for **Bybit API Secret**: Enter test secret (e.g., `test_secret_456`)
   - When prompted for **Discord Webhook**: Enter test webhook (e.g., `https://discord.com/api/webhooks/123456/abcdef`) or press Enter to skip
   - When prompted to **Start service now**: Enter `N` (we'll test this separately)

### Expected Outcomes

✅ **Pre-flight checks pass:**
- Python3, pip, git, sudo detected

✅ **Directory structure created:**
- `/opt/xsmom-bot` (or `$HOME/xsmom-bot-app` if /opt not available)
- `config/`, `src/`, `logs/`, `state/`, `data/`, `config/optimized/`, `venv/`

✅ **Files created:**
- `.env` file with secrets (mode 600, owner ubuntu)
- `config/config.yaml` (copied from example)
- `.env.example` template (if missing)

✅ **Python environment:**
- Virtualenv created at `/opt/xsmom-bot/venv`
- Dependencies installed from `requirements.txt`

✅ **Systemd services:**
- `xsmom-bot.service` installed and enabled
- `xsmom-optimizer.timer` installed and enabled
- `xsmom-meta-trainer.timer` installed and enabled

✅ **Validation/smoke test:**
- Core modules import successfully
- Config loads without errors

✅ **Final output:**
```
=== Installation Complete ===
Installation directory: /opt/xsmom-bot
Config file: /opt/xsmom-bot/config/config.yaml
Secrets file: /opt/xsmom-bot/.env (mode 600, owner ubuntu)
⚠️  Remember: Never commit .env to a public repository!
```

---

## Test Case 2: Idempotency (Re-run Installation)

### Steps

1. **Run installer again (without changes):**
   ```bash
   cd xsmom-bot
   ./install.sh
   ```

2. **When prompted to overwrite .env:**
   - Enter `N` (keep existing)

### Expected Outcomes

✅ **Venv reused:**
- Existing venv detected and reused (not recreated)

✅ **Secrets preserved:**
- Existing `.env` file kept (user chose not to overwrite)

✅ **Services updated:**
- Systemd services reinstalled/updated (idempotent)

✅ **No errors:**
- Installation completes successfully without breaking existing setup

---

## Test Case 3: Non-Interactive Mode

### Steps

1. **Set environment variables:**
   ```bash
   export RUN_NONINTERACTIVE=1
   export START_NOW=no
   ```

2. **Run installer:**
   ```bash
   ./install.sh
   ```

### Expected Outcomes

✅ **No prompts:**
- Script runs without user interaction

✅ **Secrets not overwritten:**
- If `.env` exists, it's kept (no prompts)

✅ **Services not started:**
- `START_NOW=no` prevents service start

---

## Test Case 4: Missing Prerequisites

### Steps

1. **Remove Python (or simulate missing dependency):**
   ```bash
   # Don't actually remove Python, just test error handling
   # Simulate by temporarily renaming python3
   sudo mv /usr/bin/python3 /usr/bin/python3.bak
   ```

2. **Run installer:**
   ```bash
   ./install.sh
   ```

3. **Restore Python:**
   ```bash
   sudo mv /usr/bin/python3.bak /usr/bin/python3
   ```

### Expected Outcomes

✅ **Error caught early:**
- Script exits with clear error: "Python 3 not found. Install python3 first."

✅ **No partial installation:**
- No directories/files created before error

---

## Test Case 5: Config Validation

### Steps

1. **After installation, test config loading:**
   ```bash
   cd /opt/xsmom-bot
   source venv/bin/activate
   python -c "from src.config import load_config; cfg = load_config('config/config.yaml'); print('Config loaded successfully')"
   ```

2. **Test exchange wrapper initialization (without API calls):**
   ```bash
   python -c "
   import os
   os.environ['BYBIT_API_KEY'] = 'test_key'
   os.environ['BYBIT_API_SECRET'] = 'test_secret'
   from src.exchange import ExchangeWrapper
   from src.config import load_config
   cfg = load_config('config/config.yaml')
   ex = ExchangeWrapper(cfg.exchange)
   print('Exchange wrapper initialized successfully')
   ex.close()
   "
   ```

### Expected Outcomes

✅ **Config loads:**
- No Pydantic validation errors

✅ **Exchange wrapper initializes:**
- CCXT wrapper created (even if API calls fail)

---

## Test Case 6: Service Start (Dry-Run Mode)

### Steps

1. **Verify service file exists:**
   ```bash
   sudo systemctl cat xsmom-bot.service
   ```

2. **Check service status:**
   ```bash
   sudo systemctl status xsmom-bot.service
   ```

3. **Start service:**
   ```bash
   sudo systemctl start xsmom-bot.service
   ```

4. **Check logs:**
   ```bash
   sudo journalctl -u xsmom-bot.service -f --no-pager | head -20
   ```

5. **Stop service:**
   ```bash
   sudo systemctl stop xsmom-bot.service
   ```

### Expected Outcomes

✅ **Service file correct:**
- References `/opt/xsmom-bot/.env`
- References `/opt/xsmom-bot/venv/bin/python`
- Has correct `User` and `Group` settings

✅ **Service starts:**
- No immediate crashes
- Logs show bot initializing (may fail on API connection, which is expected without valid keys)

✅ **Service stops cleanly:**
- `systemctl stop` succeeds

---

## Test Case 7: Secret File Security

### Steps

1. **Check .env file permissions:**
   ```bash
   ls -la /opt/xsmom-bot/.env
   ```

2. **Verify owner:**
   ```bash
   stat -c '%U:%G %a' /opt/xsmom-bot/.env
   ```

3. **Test read access:**
   ```bash
   sudo -u ubuntu cat /opt/xsmom-bot/.env
   sudo cat /opt/xsmom-bot/.env  # Should fail if permissions are correct
   ```

### Expected Outcomes

✅ **Permissions correct:**
- Mode: `600` (rw-------)
- Owner: `ubuntu:ubuntu` (or configured RUN_AS)

✅ **Read access:**
- User `ubuntu` can read
- Root/other users cannot read (or have restrictive access)

---

## Quick Smoke Test Script

Save this as `test_install.sh` and run after installation:

```bash
#!/bin/bash
set -euo pipefail

APP_DIR="${1:-/opt/xsmom-bot}"

echo "=== Testing Installation ==="

# Check directories
echo "Checking directories..."
for dir in config src logs state data config/optimized venv; do
  if [ -d "${APP_DIR}/${dir}" ]; then
    echo "✓ ${dir}/ exists"
  else
    echo "✗ ${dir}/ missing"
    exit 1
  fi
done

# Check files
echo "Checking files..."
for file in .env config/config.yaml venv/bin/python requirements.txt; do
  if [ -f "${APP_DIR}/${file}" ]; then
    echo "✓ ${file} exists"
  else
    echo "✗ ${file} missing"
    exit 1
  fi
done

# Check .env permissions
echo "Checking .env permissions..."
perms=$(stat -c '%a' "${APP_DIR}/.env")
if [ "${perms}" = "600" ]; then
  echo "✓ .env has correct permissions (600)"
else
  echo "✗ .env has incorrect permissions (${perms}, expected 600)"
  exit 1
fi

# Check Python imports
echo "Testing Python imports..."
"${APP_DIR}/venv/bin/python" -c "
import sys
sys.path.insert(0, '${APP_DIR}')
from src.config import load_config
from src.exchange import ExchangeWrapper
print('✓ Core modules import successfully')
"

# Check config loading
echo "Testing config loading..."
"${APP_DIR}/venv/bin/python" -c "
import sys
sys.path.insert(0, '${APP_DIR}')
from src.config import load_config
cfg = load_config('${APP_DIR}/config/config.yaml')
print(f'✓ Config loaded successfully (universe: {cfg.exchange.max_symbols} symbols)')
"

# Check systemd services
if command -v systemctl &> /dev/null; then
  echo "Checking systemd services..."
  for service in xsmom-bot.service xsmom-optimizer.timer xsmom-meta-trainer.timer; do
    if systemctl list-unit-files | grep -q "${service}"; then
      echo "✓ ${service} installed"
    else
      echo "✗ ${service} not found"
    fi
  done
fi

echo ""
echo "=== All Tests Passed ==="
```

**Usage:**
```bash
chmod +x test_install.sh
./test_install.sh /opt/xsmom-bot
```

---

## Common Issues and Solutions

### Issue: "Python 3 not found"

**Solution:**
```bash
sudo apt-get install python3 python3-pip python3-venv
```

### Issue: "sudo not found"

**Solution:**
- Run as root, or install sudo

### Issue: ".env file permissions incorrect"

**Solution:**
```bash
sudo chown ubuntu:ubuntu /opt/xsmom-bot/.env
sudo chmod 600 /opt/xsmom-bot/.env
```

### Issue: "Config validation failed"

**Solution:**
- Check `config/config.yaml` for YAML syntax errors
- Ensure all required fields are present (compare with `config.yaml.example`)

### Issue: "Service fails to start"

**Solution:**
- Check logs: `sudo journalctl -u xsmom-bot.service -n 50`
- Verify `.env` has valid API keys
- Check config file path in service file

---

## Expected Install Time

- **Fresh install:** ~2-5 minutes (depending on internet speed for pip installs)
- **Re-run (idempotent):** ~30 seconds (skips venv creation and dependency install)

---

## Success Criteria

✅ Installation completes without errors  
✅ All required directories and files created  
✅ Secrets stored securely (mode 600)  
✅ Python environment functional (imports work)  
✅ Config loads without validation errors  
✅ Systemd services installed and enabled  
✅ Installation is idempotent (safe to re-run)  
✅ Script handles missing prerequisites gracefully  

---

**Last Updated:** 2025-01-XX

