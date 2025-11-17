# Contributing

## Overview

Thank you for your interest in contributing to xsmom-bot!

**Motto:** **MAKE MONEY** — but with clear, maintainable, and well-documented code.

---

## Getting Started

1. **Read the docs:**
   - Start with [`../start_here.md`](../start_here.md)
   - Review [`../kb/framework_overview.md`](../kb/framework_overview.md) for framework understanding
   - Check [`style_guide.md`](style_guide.md) for code and documentation conventions

2. **Set up development environment:**
   - Clone repository
   - Create virtual environment
   - Install dependencies (`pip install -r requirements.txt`)
   - Configure `.env` and `config/config.yaml`

3. **Test on testnet first:**
   - Always test changes on testnet (`exchange.testnet: true`)
   - Run backtests to verify changes
   - Test live on testnet before production

---

## Development Workflow

### Making Changes

1. **Create feature branch:**
   ```bash
   git checkout -b feature/my-feature
   ```

2. **Make changes:**
   - Follow code style (see [`style_guide.md`](style_guide.md))
   - Add type hints where possible
   - Add docstrings for new functions/classes
   - Update documentation if framework changes

3. **Test changes:**
   - Run backtests to verify changes
   - Test on testnet if changing live trading logic
   - Update KB if framework changes (see [`../kb/knowledge_base.md`](../kb/knowledge_base.md))

4. **Update documentation:**
   - If framework changes: Update `docs/kb/framework_overview.md` and `docs/kb/change_log_architecture.md`
   - If config changes: Regenerate `docs/reference/config_reference.md` (`python -m tools.update_kb`)
   - If architecture changes: Update relevant docs in `docs/architecture/`

5. **Commit changes:**
   - Use clear commit messages
   - Include documentation updates in same commit

6. **Submit pull request:**
   - Describe changes clearly
   - Link to relevant issues
   - Include test results (backtests, testnet runs)

---

## Code Style

### Python

**Guidelines:**
- Use type hints where possible
- Follow PEP 8 (use `black` or similar formatter)
- Add docstrings (Google style) for functions/classes
- Keep functions small (< 100 lines if possible)
- Use meaningful variable names

**Example:**
```python
def compute_signal(
    returns: pd.DataFrame,
    lookbacks: List[int],
    weights: List[float],
) -> pd.Series:
    """
    Compute weighted momentum signal.
    
    Args:
        returns: Asset returns (index: datetime, columns: symbols)
        lookbacks: Lookback periods (hours/bars)
        weights: Weights for each lookback
    
    Returns:
        Signal Series (index: datetime, values: signal scores)
    """
    # Implementation
```

### Configuration

**Guidelines:**
- Use Pydantic models for config validation
- Provide defaults in `_merge_defaults()`
- Document parameters in docstrings
- Keep parameter count low (avoid overfitting)

---

## Documentation Standards

### When to Update Docs

**Always update:**
- Framework changes (strategy, risk, optimizer) → Update `kb/framework_overview.md` and `kb/change_log_architecture.md`
- Config changes → Regenerate `reference/config_reference.md`
- Architecture changes → Update relevant docs in `architecture/`

**May update:**
- Bug fixes (if they change behavior) → Update relevant usage/operations docs
- Performance improvements (if they change architecture) → Update architecture docs

**Don't update:**
- Minor bug fixes (that don't change behavior)
- Documentation-only changes (update docs, not KB)

### Documentation Style

**Guidelines:**
- Be clear and concise (see [`style_guide.md`](style_guide.md))
- Use examples (copy-paste ready)
- Cross-reference related docs
- Keep KB current (matches actual code)

---

## Testing

### Backtests

**Before submitting:**
- Run backtests to verify changes don't break existing functionality
- Compare performance to baseline (ensure no degradation)

**Command:**
```bash
python -m src.main backtest --config config/config.yaml
```

### Testnet

**Before submitting:**
- Test changes on testnet (`exchange.testnet: true`)
- Monitor for errors and issues
- Verify behavior matches expectations

**Command:**
```bash
python -m src.main live --config config/config.yaml
```

---

## KB Maintenance

### When Framework Changes

1. **Update `kb/framework_overview.md`**:
   - Reflect current framework state
   - Update relevant sections (strategy, risk, optimizer, etc.)
   - Cross-reference related docs

2. **Add entry to `kb/change_log_architecture.md`**:
   - Date, type, changes, rationale, impact
   - Link to relevant files/modules

3. **Regenerate auto-generated docs:**
   ```bash
   python -m tools.update_kb
   ```

4. **Update related docs:**
   - Architecture docs (if architecture changes)
   - Usage guides (if usage changes)
   - Reference docs (if parameters change)

See [`style_guide.md`](style_guide.md) for KB maintenance guidelines.

---

## Pull Request Guidelines

### Required

- [ ] Code follows style guidelines
- [ ] Type hints added where possible
- [ ] Docstrings added for new functions/classes
- [ ] Backtests pass (if applicable)
- [ ] Testnet tested (if changing live trading logic)
- [ ] Documentation updated (if framework changes)
- [ ] KB updated (if framework changes)
- [ ] Commit messages are clear

### Recommended

- [ ] Unit tests added (if applicable)
- [ ] Performance benchmarks (if optimizing)
- [ ] Migration guide (if breaking changes)

---

## Questions?

- **Code questions**: Check [`../kb/framework_overview.md`](../kb/framework_overview.md) or relevant architecture docs
- **Documentation questions**: Check [`style_guide.md`](style_guide.md)
- **Process questions**: Check this guide or open an issue

---

**Motto: MAKE MONEY** — but with clear, maintainable, and well-documented contributions. 📈

