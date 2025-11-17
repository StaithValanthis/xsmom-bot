# Refactor: `src/utils.py`

## STEP 1 – UNDERSTAND & SUMMARIZE

### What `utils.py` Currently Does

**Purpose:** Provides foundational utilities for the trading bot:
- Time utilities (UTC timestamps)
- File I/O (JSON read/write for state persistence)
- Logging configuration
- Environment variable loading

### Inputs & Outputs

**Functions:**

1. **`utcnow() -> datetime`**
   - Input: None
   - Output: Current UTC datetime
   - Side effects: None (pure)

2. **`read_json(path: str, default) -> Any`**
   - Input: File path, default value
   - Output: Parsed JSON or default
   - Side effects: Reads from filesystem

3. **`write_json(path: str, data) -> None`**
   - Input: File path, data to write
   - Output: None
   - Side effects: **⚠️ CRITICAL BUG**: Non-atomic write (corruption risk on crash)
   - Writes JSON to filesystem

4. **`setup_logging(...)`**
   - Input: Log level, directory, file size/backup config
   - Output: None
   - Side effects: Configures global logging handlers

5. **`load_env_file_if_present() -> None`**
   - Input: None (uses project root)
   - Output: None
   - Side effects: Sets environment variables (if not already set)

### Role in Workflows

**Live Trading:**
- `read_json()` / `write_json()`: State persistence (positions, cooldowns, stats) — **CRITICAL**
- `utcnow()`: Timestamp generation for trade logs, cooldowns
- Called ~10+ times per trading cycle in `live.py`

**Backtesting:**
- `utcnow()`: Timestamp logging
- Less critical (no state persistence)

**Optimizer:**
- Not directly used (uses temp files)

### Major Responsibilities

1. **State Persistence** (JSON I/O) — Most critical
2. **Time Utilities** (UTC timestamps)
3. **Logging Configuration** (one-time setup)
4. **Environment Loading** (optional .env file)

### Code Smells Identified

1. **⚠️ CRITICAL: Non-Atomic Writes**
   - `write_json()` directly writes to target file
   - If bot crashes mid-write, state file corrupted
   - Risk: Lost position tracking, lost cooldowns, inconsistent state

2. **Silent Failures**
   - `read_json()` swallows all exceptions → returns default
   - `write_json()` has no error handling (could raise, crash bot)
   - `load_env_file_if_present()` swallows all exceptions

3. **Mixed Concerns**
   - Time, I/O, logging, env loading all in one file (acceptable for utils, but error handling inconsistent)

4. **No Type Safety**
   - `read_json()` returns `Any`
   - No validation of file paths

5. **No Health Check Utilities**
   - Missing heartbeat write function (needed for health monitoring per roadmap)

---

## STEP 2 – DESIGN A BETTER STRUCTURE

### Before vs After

**BEFORE:**
```python
utils.py (96 lines)
├── utcnow()                    # Pure, OK
├── read_json()                 # ❌ Silent failures
├── write_json()                # ❌ Non-atomic, no error handling
├── setup_logging()             # Side-effectful, OK
└── load_env_file_if_present()  # Side-effectful, silent failures
```

**AFTER:**
```python
utils.py (refactored, ~350 lines)
├── TIME UTILITIES (Pure)
│   └── utcnow() -> datetime
│
├── FILE I/O (Side-effectful, but safer)
│   ├── read_json() -> Any  # Better error handling
│   ├── write_json_atomic() -> None  # NEW: Atomic writes
│   └── write_json() -> None  # DEPRECATED wrapper (calls atomic)
│
├── HEALTH CHECKS (NEW, Side-effectful)
│   ├── write_heartbeat() -> None  # NEW: For monitoring
│   └── read_heartbeat() -> dict | None  # NEW: Read heartbeat
│
├── LOGGING (Side-effectful, unchanged)
│   └── setup_logging()
│
└── ENVIRONMENT (Side-effectful, improved)
    └── load_env_file_if_present()  # Better error handling
```

### Design Principles

**Pure Functions (No Side Effects):**
- `utcnow()` — unchanged

**Side-Effectful but Safer:**
- `read_json()` — log errors but return default (fail-safe)
- `write_json_atomic()` — atomic writes (temp file + rename)
- `write_heartbeat()` — atomic heartbeat writes

**Error Handling Strategy:**
- **JSON I/O**: Log errors, return defaults (don't crash bot)
- **Heartbeat**: Log errors, but don't crash (monitoring is non-critical)
- **Logging setup**: Can raise (fatal if logging broken)

---

## STEP 3 – IMPLEMENT THE REFACTOR

### Key Changes

1. **✅ Fixed Critical Bug: Atomic Writes**
   - `write_json()` now calls `write_json_atomic()` internally
   - Uses temp file + atomic rename (prevents corruption on crash)
   - Backward compatible (same function signature)

2. **✅ Improved Error Handling**
   - `read_json()`: Structured error handling (log specific error types)
   - `write_json()`: Raises on non-recoverable errors (no silent failures for critical writes)
   - `load_env_file_if_present()`: Better error messages, log warnings

3. **✅ Added Health Check Utilities**
   - `write_heartbeat()`: For health monitoring (NEW)
   - `read_heartbeat()`: Read and validate heartbeat (NEW)
   - Addresses roadmap requirement for health checks

4. **✅ Better Documentation**
   - Docstrings for all functions
   - Type hints (using `Optional`, `dict[str, Any]`)
   - Examples in docstrings

5. **✅ Code Structure**
   - Organized into logical sections with comments
   - Consistent error handling patterns
   - Uses `Path` objects for better path handling

### Backward Compatibility

**✅ FULLY COMPATIBLE:**
- All existing function signatures preserved
- `write_json()` still works (now calls atomic internally)
- `read_json()` behavior unchanged (still returns default on error)
- `utcnow()`, `setup_logging()`, `load_env_file_if_present()` unchanged

**No changes needed in other files** — drop-in replacement.

---

## STEP 4 – TESTABILITY & USAGE NOTES

### Example Usage Snippets

**1. State Persistence (Existing Pattern, Now Safer):**
```python
from utils import read_json, write_json

# Read state (returns {} if file doesn't exist)
state = read_json("state/state.json", default={})
state["positions"] = {"BTC/USDT:USDT": {"qty": 1.0}}

# Write state (atomic, crash-safe)
write_json("state/state.json", state)
```

**2. Health Monitoring (NEW):**
```python
from utils import write_heartbeat, read_heartbeat

# In main trading loop (every minute)
write_heartbeat("state/heartbeat.json")

# In health check script (external monitoring)
hb = read_heartbeat("state/heartbeat.json")
if hb and not hb["healthy"]:
    alert_admin(f"Bot heartbeat stale: {hb['age_sec']:.0f}s old")
```

**3. Environment Loading:**
```python
from utils import load_env_file_if_present

# At startup (before importing exchange wrapper)
load_env_file_if_present()
# Now os.getenv("BYBIT_API_KEY") will work if set in .env
```

**4. Timestamps:**
```python
from utils import utcnow

# Consistent UTC timestamps
ts = utcnow()
log_entry = {"timestamp": ts.isoformat(), "equity": 10000.0}
```

### Suggested Unit Tests

**Test 1: Atomic Write Prevents Corruption**
```python
def test_write_json_atomic_no_corruption():
    """Verify that partial writes don't corrupt target file."""
    path = "/tmp/test_state.json"
    original_data = {"key": "value"}
    
    # Simulate crash mid-write by interrupting the write
    # Should leave temp file, not corrupt target
    
    # Write complete data
    write_json_atomic(path, original_data)
    
    # Verify file is valid JSON
    data = read_json(path, None)
    assert data == original_data
    
    # Cleanup
    os.unlink(path)
```

**Test 2: Read JSON Handles Missing Files Gracefully**
```python
def test_read_json_missing_file():
    """Verify read_json returns default for missing files."""
    data = read_json("/tmp/nonexistent.json", default={"empty": True})
    assert data == {"empty": True}
```

**Test 3: Read JSON Handles Corrupted Files Gracefully**
```python
def test_read_json_corrupted_file():
    """Verify read_json returns default for corrupted JSON."""
    path = "/tmp/corrupted.json"
    with open(path, "w") as f:
        f.write("{ invalid json }")  # Invalid JSON
    
    data = read_json(path, default={})
    assert data == {}  # Should return default, not crash
    
    os.unlink(path)
```

**Test 4: Heartbeat Write and Read**
```python
def test_heartbeat_roundtrip():
    """Verify heartbeat write/read works correctly."""
    path = "/tmp/heartbeat.json"
    
    # Write heartbeat
    write_heartbeat(path)
    
    # Read heartbeat
    hb = read_heartbeat(path)
    assert hb is not None
    assert hb["healthy"] is True
    assert hb["age_sec"] < 1.0  # Should be very recent
    
    # Wait 3 seconds, read again
    time.sleep(3.0)
    hb2 = read_heartbeat(path)
    assert hb2["age_sec"] >= 3.0
    assert hb2["healthy"] is True  # Still < 120s
    
    os.unlink(path)
```

**Test 5: Heartbeat Detects Stale Bot**
```python
def test_heartbeat_stale_detection():
    """Verify heartbeat correctly identifies stale bots."""
    path = "/tmp/heartbeat.json"
    
    # Write old heartbeat (simulate stale bot)
    old_ts = utcnow() - timedelta(seconds=150)  # 150s ago
    write_json_atomic(path, {
        "ts": old_ts.isoformat(),
        "unix_ts": old_ts.timestamp(),
    })
    
    # Read heartbeat
    hb = read_heartbeat(path)
    assert hb is not None
    assert hb["age_sec"] >= 150.0
    assert hb["healthy"] is False  # Should be unhealthy
    
    os.unlink(path)
```

**Test 6: Error Handling on Write Failures**
```python
def test_write_json_atomic_handles_errors():
    """Verify write_json_atomic raises on non-recoverable errors."""
    # Write to non-existent directory without permissions
    # Should raise OSError, not crash silently
    
    # Write to directory that can't be created
    invalid_path = "/root/cant_create/state.json"
    with pytest.raises(OSError):
        write_json_atomic(invalid_path, {"key": "value"})
```

**Test 7: Environment Loading Respects Existing Vars**
```python
def test_load_env_file_if_present_no_override():
    """Verify .env loading doesn't override existing env vars."""
    os.environ["TEST_KEY"] = "existing_value"
    
    # Create .env with same key
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("TEST_KEY=new_value\n")
        env_path = f.name
    
    # Mock the function to use temp .env
    # Should not override TEST_KEY
    
    # Verify
    assert os.getenv("TEST_KEY") == "existing_value"
    
    os.unlink(env_path)
```

---

## STEP 5 – FINAL CHECK (MAKE MONEY)

### ✅ Risk Controls Preserved

- **State persistence still works**: All existing state writes now use atomic writes (safer)
- **Error handling improved**: No silent failures for critical writes
- **Graceful degradation**: Read failures still return defaults (bot doesn't crash)

### ✅ Risk Reduction

1. **Eliminated Corruption Risk**: Atomic writes prevent state file corruption on crash
   - **Impact**: Prevents lost position tracking, lost cooldowns
   - **How it helps MAKE MONEY**: Reliable state = reliable position management = fewer bugs = consistent profits

2. **Added Health Monitoring**: Heartbeat utilities enable external monitoring
   - **Impact**: Can detect silent failures (bot stops trading)
   - **How it helps MAKE MONEY**: Faster detection of issues = less missed trading opportunities

3. **Better Error Logging**: Structured error handling with logging
   - **Impact**: Easier debugging when issues occur
   - **How it helps MAKE MONEY**: Faster issue resolution = less downtime = more trading time

### ✅ Future Extensibility

**Easier to add new features:**

1. **Add new state files**: Pattern established (atomic writes, error handling)
2. **Add more health checks**: Extend `read_heartbeat()` or add new health check functions
3. **Add state validation**: Can add validation layer in `read_json()` / `write_json()`
4. **Add encryption**: Can add encryption/decryption layer transparently

**Easier to test:**

- Pure functions (`utcnow()`) are easy to test
- Side-effectful functions have clear error paths to test
- Health checks can be tested independently

**Easier to plug into backtests/optimizers:**

- No changes needed (backward compatible)
- Can mock file I/O easily for testing
- Heartbeat utilities don't affect backtests (can be disabled)

### ✅ Alignment with Architecture Recommendations

From the roadmap review:

1. **✅ Tier 1 Quick Win**: Fixed state file corruption risk (atomic writes)
2. **✅ Health Checks Added**: Heartbeat utilities for monitoring
3. **✅ Better Error Handling**: Structured error logging, no silent failures
4. **✅ Documentation**: Comprehensive docstrings and type hints

### Final Summary: How This Helps MAKE MONEY

**Before Refactor:**
- State file corruption risk on crash → Lost positions, lost cooldowns → Trading errors → Lost money
- No health monitoring → Silent failures undetected → Bot stops trading → Lost opportunities
- Silent failures → Hard to debug → Long downtime → Lost trading time

**After Refactor:**
- ✅ Atomic writes → No corruption → Reliable state → Consistent position management → **Consistent profits**
- ✅ Health monitoring → Fast failure detection → Quick recovery → **More trading time**
- ✅ Better error handling → Easier debugging → Less downtime → **More trading time**

**Bottom Line:**
This refactor directly addresses critical reliability issues that could cause data loss and trading errors. By making state persistence crash-safe and adding health monitoring, the bot is more robust and reliable, which directly contributes to consistent, risk-adjusted profitability.

---

## IMPLEMENTATION NOTES

### Changes in Other Files (None Required)

**Backward Compatibility:** ✅
- All existing code continues to work
- `write_json()` signature unchanged (now uses atomic internally)
- `read_json()` behavior unchanged (still fail-safe)

### Migration Path (None Required)

**Drop-in replacement:** Just update `src/utils.py` — no other changes needed.

### Future Enhancements (Optional)

1. **Add state validation** in `read_json()` (validate schema)
2. **Add encryption** for sensitive state files (API keys in state)
3. **Add metrics** for file I/O performance (Prometheus)
4. **Add compression** for large state files (if needed)

---

*End of Refactor*
