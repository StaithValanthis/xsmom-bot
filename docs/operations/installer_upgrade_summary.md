# Install Script Upgrade Summary

## Overview

The `install.sh` script has been upgraded to be a **true one-shot installer** that guides users through the entire setup process, including interactive prompts for required secrets.

---

## What Was Wrong/Missing

### Original Install Script Issues

1. **No interactive prompts for secrets**
   - Script created `.env` from `.env.example`, but `.env.example` didn't exist in repo
   - Users had to manually edit `.env` after installation
   - No validation that secrets were actually set

2. **Missing required directories**
   - `data/` directory (needed for rollout state: `data/config_rollout_state.json`)
   - `config/optimized/` directory (needed for optimizer output)

3. **No validation or smoke test**
   - No check that Python modules import correctly
   - No validation that config loads without errors
   - Installation could succeed but bot still fail on first run

4. **Limited pre-flight checks**
   - Didn't verify Python, pip, git, sudo before starting
   - Could fail mid-installation with unclear errors

5. **Not fully idempotent**
   - Recreating venv every time (slow)
   - No check if `.env` already has secrets before overwriting

---

## What Changed

### ✅ Added Pre-Flight Checks

- **Verifies prerequisites:** Python3, pip, git, sudo, systemctl
- **Fails fast:** Exits early with clear error messages if prerequisites missing

### ✅ Interactive Secret Prompts

- **Prompts for required secrets:**
  - Bybit API Key (visible input)
  - Bybit API Secret (hidden input with `read -s`)
  - Discord Webhook URL (optional)

- **Idempotent secret handling:**
  - Checks if `.env` already exists with valid keys
  - Asks user whether to overwrite (unless non-interactive mode)
  - Creates `.env.example` template if missing

### ✅ Added Missing Directories

- **Creates all required directories:**
  - `data/` (for rollout state)
  - `config/optimized/` (for optimized configs)
  - `reports/` (for optimizer reports)

- **Ensures proper ownership:**
  - All directories owned by `RUN_AS:RUN_GROUP` (default: ubuntu:ubuntu)

### ✅ Added Validation & Smoke Test

- **Config validation:**
  - Tests that config YAML loads without Pydantic errors
  - Validates required fields are present

- **Module import test:**
  - Verifies core Python modules (`src.config`, `src.exchange`) import correctly
  - Catches missing dependencies early

- **Security validation:**
  - Verifies `.env` file has correct permissions (mode 600)
  - Ensures proper ownership

### ✅ Improved Idempotency

- **Venv reuse:**
  - Checks if venv exists before creating
  - Reuses existing venv (faster re-runs)

- **Config preservation:**
  - Doesn't overwrite `config/config.yaml` if it exists
  - Asks before overwriting `.env` if it has valid secrets

- **Safe re-runs:**
  - Can run multiple times without breaking existing setup
  - Updates services if needed, preserves user data

### ✅ Enhanced Error Handling

- **Graceful degradation:**
  - Warns instead of failing if optional components missing (git, systemctl)
  - Continues installation even if some steps fail (with warnings)

- **Clear error messages:**
  - Specific error messages for each failure mode
  - Suggests solutions for common issues

### ✅ Better User Experience

- **Clear progress indicators:**
  - Color-coded output (green for success, yellow for warnings, red for errors)
  - Step-by-step progress messages

- **Helpful next steps:**
  - Prints summary of what was installed
  - Provides commands for common tasks (start service, check logs)

- **Security reminders:**
  - Warns users not to commit `.env` to public repos
  - Reminds about secret file permissions

---

## Diff Summary (Conceptual Changes)

### New Functions

- `check_requirements()` - Pre-flight system checks
- `ensure_env_example()` - Creates `.env.example` template if missing
- `prompt_secrets()` - Interactive secret collection and `.env` file creation
- `validate_installation()` - Smoke tests and validation

### Enhanced Functions

- `normalize_destination_tree()` - Now creates `data/` and `config/optimized/`
- `seed_local_if_missing()` - Creates more directories including `data/`

### Main Flow Changes

1. **Pre-flight checks** run first (fail fast if prerequisites missing)
2. **Interactive prompts** collect secrets before installation completes
3. **Validation** runs at end to verify installation succeeded
4. **Idempotent operations** check for existing files before creating

---

## New Features

### Interactive Secret Collection

**Before:**
```bash
# User had to manually edit .env after installation
./install.sh
# Then manually: nano /opt/xsmom-bot/.env
```

**After:**
```bash
# Script prompts for secrets during installation
./install.sh
[?] Bybit API Key (required): <user enters key>
[?] Bybit API Secret (required, hidden): <user enters secret>
[?] Discord Webhook URL (optional): <user enters webhook or skips>
```

### Non-Interactive Mode

**Usage:**
```bash
RUN_NONINTERACTIVE=1 START_NOW=no ./install.sh
```

- Skips all prompts
- Keeps existing `.env` if present
- Useful for automated deployments

### Smoke Test

**Automatically runs:**
- Tests Python imports (`src.config`, `src.exchange`)
- Validates config loading
- Catches dependency issues early

---

## Security Improvements

### ✅ Secret File Protection

- **`.env` file permissions:** Mode 600 (rw-------)
- **Ownership:** Restricted to `RUN_AS:RUN_GROUP`
- **Warnings:** Reminds users not to commit secrets

### ✅ Secure Input

- **Hidden secret input:** Uses `read -s` for API secret
- **No echo:** API secret not displayed on terminal

---

## Testing

See [`installer_test_plan.md`](installer_test_plan.md) for comprehensive test plan.

**Quick test:**
```bash
# Fresh installation test
./install.sh

# Idempotency test (re-run)
./install.sh

# Non-interactive test
RUN_NONINTERACTIVE=1 ./install.sh
```

---

## Migration Guide

### For Existing Installations

**If you already have `/opt/xsmom-bot` installed:**

1. **Run updated installer:**
   ```bash
   cd /path/to/xsmom-bot
   ./install.sh
   ```

2. **When prompted to overwrite `.env`:**
   - Enter `N` to keep existing secrets
   - Or `Y` to update secrets

3. **Missing directories will be created:**
   - `data/` (for rollout state)
   - `config/optimized/` (for optimized configs)

4. **Services will be updated:**
   - Systemd units reinstalled (idempotent)
   - No data loss

---

## Usage Examples

### Fresh Installation (Interactive)

```bash
# Clone repo
git clone <repo-url> xsmom-bot
cd xsmom-bot

# Make executable
chmod +x install.sh

# Run installer
./install.sh

# Follow prompts:
# - Enter Bybit API Key
# - Enter Bybit API Secret (hidden)
# - Enter Discord Webhook (optional)
# - Choose whether to start service
```

### Non-Interactive Installation

```bash
# Set environment variables
export RUN_NONINTERACTIVE=1
export START_NOW=no
export RUN_AS=ubuntu

# Run installer
./install.sh
```

### Custom Installation Path

```bash
# If /opt not available, installer falls back to $HOME
./install.sh

# Or set custom path (requires script modification)
```

---

## File Changes

### Modified Files

- **`install.sh`** - Complete rewrite with interactive prompts, validation, and improved idempotency

### New Files

- **`.env.example`** - Template for `.env` file (created automatically if missing)
- **`docs/operations/installer_test_plan.md`** - Comprehensive test plan
- **`docs/operations/installer_upgrade_summary.md`** - This file

---

## Next Steps

After installation:

1. **Review config:**
   ```bash
   nano /opt/xsmom-bot/config/config.yaml
   ```

2. **Verify secrets:**
   ```bash
   sudo -u ubuntu cat /opt/xsmom-bot/.env
   ```

3. **Test on testnet:**
   - Set `exchange.testnet: true` in `config/config.yaml`
   - Use testnet API keys

4. **Start bot:**
   ```bash
   sudo systemctl start xsmom-bot.service
   ```

5. **Monitor logs:**
   ```bash
   sudo journalctl -u xsmom-bot.service -f
   ```

---

**Last Updated:** 2025-01-XX

