# Documentation Style Guide

## Overview

This guide defines conventions for writing and maintaining documentation in xsmom-bot.

**Motto:** Clear, concise, practical documentation that makes it easy to understand and maintain the system.

---

## Tone & Voice

### Clarity First

- **Be clear and direct** - Avoid jargon unless necessary (define terms)
- **Be concise** - Get to the point quickly (no fluff)
- **Be practical** - Focus on actionable information (how to use, not just what it is)

### Target Audience

**Primary:**
- Quant developers joining the project
- Operators running the bot in production
- Future you returning in 6-12 months

**Secondary:**
- External contributors (if open-sourced)
- New team members

### Writing Style

**Do's:**
- Use active voice ("The bot fetches data" not "Data is fetched by the bot")
- Use second person ("You can run..." not "One can run...")
- Be specific ("Runs every 2 seconds" not "Runs frequently")
- Use examples ("See example below" not "See example above")

**Don'ts:**
- Avoid passive voice (unless clearer)
- Avoid jargon without definitions
- Avoid vague statements ("works well" → "improves Sharpe by 0.2")
- Avoid assumptions ("Obviously..." → Explain explicitly)

---

## Markdown Conventions

### Headings

**Structure:**
- `#` - Document title (one per document)
- `##` - Major sections
- `###` - Subsections
- `####` - Sub-subsections (use sparingly)

**Example:**
```markdown
# Document Title

## Section 1

### Subsection 1.1

#### Sub-subsection 1.1.1 (rarely needed)
```

### Code Blocks

**Python:**
```python
from src.config import load_config

cfg = load_config("config/config.yaml")
```

**Bash:**
```bash
python -m src.main live --config config/config.yaml
```

**YAML:**
```yaml
strategy:
  signal_power: 1.35
  lookbacks: [12, 24, 48, 96]
```

**Inline Code:**
- Use `` `backticks` `` for function names, parameters, file paths
- Example: `` `strategy.signal_power` ``

### Lists

**Bullet Lists:**
```markdown
- Item 1
- Item 2
- Item 3
```

**Numbered Lists:**
```markdown
1. Step 1
2. Step 2
3. Step 3
```

**Nested Lists:**
```markdown
- Item 1
  - Sub-item 1.1
  - Sub-item 1.2
- Item 2
```

### Emphasis

**Bold (`**text**`):**
- Use for important terms, warnings, key concepts
- Example: **MAKE MONEY**, **WARNING**, **DO NOT**

**Italic (`*text*`):**
- Use for emphasis, variable names, optional content
- Example: *optional*, *default: 1.35*, *if enabled*

### Links

**Internal Links:**
```markdown
[Link Text](relative/path/to/doc.md)
[Link Text](../parent/doc.md)
```

**External Links:**
```markdown
[Link Text](https://example.com)
```

### Tables

```markdown
| Column 1 | Column 2 | Column 3 |
|----------|----------|----------|
| Value 1  | Value 2  | Value 3  |
| Value 4  | Value 5  | Value 6  |
```

### Blockquotes

```markdown
> **Note:** Important note or warning
> **Example:** Example usage
```

---

## Documentation Structure

### File Organization

**Directory Structure:**
```
docs/
├── README.md                      # Docs index/landing page
├── start_here.md                  # Reading guide
├── overview/                      # Overview docs
│   ├── project_overview.md
│   ├── quickstart.md
│   └── glossary.md
├── architecture/                  # Architecture docs
│   ├── high_level_architecture.md
│   ├── strategy_logic.md
│   └── risk_management.md
├── usage/                         # Usage guides
│   ├── live_trading.md
│   ├── optimizer.md
│   └── discord_notifications.md
├── operations/                    # Operations docs
│   ├── deployment_ubuntu_systemd.md
│   └── troubleshooting.md
├── reference/                     # Reference docs
│   ├── config_reference.md
│   └── cli_reference.md
├── kb/                            # Knowledge Base
│   ├── knowledge_base.md
│   ├── framework_overview.md
│   └── change_log_architecture.md
└── meta/                          # Meta docs
    ├── changelog.md
    └── style_guide.md
```

### File Naming

**Conventions:**
- Use lowercase with underscores: `live_trading.md`
- Be descriptive: `deployment_ubuntu_systemd.md` not `deploy.md`
- Group related docs: `docs/usage/optimizer.md`

---

## Content Guidelines

### Document Sections

**Standard Sections:**
1. **Title** - Clear, descriptive title
2. **Overview** - Brief summary (2-3 sentences)
3. **Main Content** - Detailed explanation
4. **Examples** - Practical examples
5. **Configuration** - Config parameters (if relevant)
6. **Troubleshooting** - Common issues (if relevant)
7. **Next Steps** - Links to related docs

**Optional Sections:**
- **Rationale** - Why design choices were made
- **Limitations** - Known limitations or constraints
- **Future Plans** - Planned improvements

### Code Examples

**Do's:**
- Use real examples (copy-paste from actual code/config)
- Include expected output
- Explain what each example does
- Show error cases (if relevant)

**Don'ts:**
- Don't use placeholder values (`TODO`, `...`)
- Don't assume reader knows context
- Don't skip error handling

**Example:**
```markdown
### Example: Running Backtest

**Command:**
```bash
python -m src.main backtest --config config/config.yaml
```

**Expected Output:**
```
=== BACKTEST (cost-aware) ===
Samples: 1440 bars  |  Universe size: 36
Total Return: 15.23% | Annualized: 42.15% | Sharpe: 1.45
Max Drawdown: -12.34% | Calmar: 3.41
```
```

### Cross-References

**Do's:**
- Link to related docs (e.g., "See [`architecture/strategy_logic.md`](architecture/strategy_logic.md)")
- Use descriptive link text ("See strategy logic" not "See this")
- Keep links up-to-date (fix broken links)

**Don'ts:**
- Don't duplicate information (link instead)
- Don't use vague links ("See above" → use specific section links)

---

## KB Maintenance

### When to Update KB

**Update `framework_overview.md`:**
- Major architectural changes (new modules, refactoring)
- Strategy changes (new signals, filters, sizing methods)
- Risk management changes (new limits, stops, controls)
- Optimizer changes (new methods, deployment logic)
- Infrastructure changes (new deployment model, monitoring)

**Update `change_log_architecture.md`:**
- Framework-level changes (architecture, strategy, risk, optimizer)
- Major feature additions or removals
- Design philosophy changes

**Do NOT update KB for:**
- Bug fixes (unless they change framework behavior)
- Performance improvements (unless they change architecture)
- Documentation updates (update relevant docs, not KB)

### KB Update Workflow

1. **Make framework change** (code/config change)
2. **Update `framework_overview.md`** (reflect current state)
3. **Add entry to `change_log_architecture.md`** (document change)
4. **Regenerate auto-generated docs** (`python -m tools.update_kb`)
5. **Update related docs** (architecture docs, config reference, etc.)
6. **Commit changes** (include KB updates in same commit)

---

## Auto-Generated Content

### Module Maps

**Location:** `docs/architecture/module_map.md` (also `docs/kb/autogenerated/module_map.md`)

**Generated by:** `tools/update_kb.py`

**When to regenerate:**
- After adding new modules to `src/`
- After changing module structure
- Before committing major changes

**How to regenerate:**
```bash
python -m tools.update_kb --skip-config-ref  # Only module map
```

### Config Reference

**Location:** `docs/reference/config_reference.md`

**Generated by:** `tools/update_kb.py`

**When to regenerate:**
- After modifying `config/config.yaml.example`
- After adding new config parameters
- Before committing config changes

**How to regenerate:**
```bash
python -m tools.update_kb --skip-module-map  # Only config ref
```

### Manual Enhancements

**Do's:**
- Add hand-written descriptions to auto-generated docs (in comments)
- Enhance parameter descriptions (if code can't infer)
- Add optimization recommendations (which params to optimize)

**Don'ts:**
- Don't manually edit auto-generated sections (they'll be overwritten)
- Don't mix auto-generated and hand-written content (use separate sections)

---

## Documentation Reviews

### Before Committing

**Checklist:**
- [ ] All links work (no broken references)
- [ ] Examples are complete (copy-paste ready)
- [ ] Code examples are tested (actually work)
- [ ] Config examples are valid (validated against schema)
- [ ] Cross-references are accurate (link to correct sections)
- [ ] KB updated (if framework changed)
- [ ] Auto-generated docs regenerated (if code/config changed)

### Review Criteria

**Clarity:**
- Can a new developer understand this?
- Can an operator use this without confusion?
- Is the reading path clear?

**Completeness:**
- Are all steps covered?
- Are examples included?
- Are edge cases mentioned?

**Accuracy:**
- Does it match the code?
- Are config examples valid?
- Are command examples correct?

---

## Common Patterns

### "See Also" Section

```markdown
---

## See Also

- [`architecture/strategy_logic.md`](architecture/strategy_logic.md) - Strategy logic
- [`reference/config_reference.md`](reference/config_reference.md) - Config reference
- [`kb/framework_overview.md`](kb/framework_overview.md) - Framework overview
```

### "Next Steps" Section

```markdown
---

## Next Steps

👉 **Read [`start_here.md`](start_here.md)** for your reading path.

👉 **Read [`architecture/strategy_logic.md`](architecture/strategy_logic.md)** for detailed strategy logic.
```

### "Troubleshooting" Section

```markdown
---

## Troubleshooting

### Issue: "No symbols after filters"

**Cause:** Universe filter too restrictive.

**Fix:** Adjust `config/config.yaml`:
```yaml
exchange:
  max_symbols: 50              # Increase
  min_usd_volume_24h: 50000000  # Decrease
```
```

---

## Questions?

- **For documentation conventions:** See this guide
- **For KB maintenance:** See [`kb/knowledge_base.md`](kb/knowledge_base.md)
- **For content questions:** Check existing docs for patterns

---

**Motto: MAKE MONEY** — but with clear, well-documented, and maintainable documentation. 📈

