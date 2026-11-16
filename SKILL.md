---
name: jroute-cli
description: Intelligent local CLI meta-router (jroute & jrc) that uses TypeSafe AI Jev SDK (system_one) and Ollaya to evaluate prompt complexity, production risk, and workspace context, then automatically selects and launches the optimal coding agent (Claude Code or Antigravity agy), sub-model, and reasoning effort level with a git-free shared memory layer (.jrc_context.md).
summary: Local System-1 CLI meta-router for Claude Code & Antigravity (agy) with automatic sub-model/effort selection and git-free .jrc_context.md shared memory.
version: "1.0.0"
tags:
  - cli
  - router
  - claude-code
  - antigravity
  - agent-orchestration
  - ollaya
  - jev
  - developer-tools
keywords:
  - jroute
  - jrc
  - meta-router
  - system-one
  - claude-code
  - agy
  - shared-memory
  - model-routing
use_cases:
  - Route prompts automatically between Claude Code and Antigravity (agy) based on task complexity and production risk
  - Dynamically calibrate sub-model and reasoning effort (low, medium, high, xhigh) per task
  - Share git-free project memory (.jrc_context.md) across Claude Code and Antigravity sessions
  - Preview routing decisions and probability distributions with --dry-run or --json
docs_url: https://github.com/efexorce/jroute-cli
---

# jroute-cli (`jroute` & `jrc`)

**jroute-cli** is a local System-1 decision router powered by the **TypeSafe AI Jev SDK** (`system_one`) and a local **Ollaya** server (`http://127.0.0.1:11435`). It inspects the developer prompt and active workspace context (`cwd`, project files, tech stack markers) in a single pass—evaluating complexity, production risk, and tool fit—to select and launch the optimal CLI agent (**Claude Code** or **Antigravity / `agy`**), sub-model, and reasoning effort level.

## Included Files

- `router.py` (`jroute`): Single-command CLI router supporting `--dry-run` (`-d`), `--print` (`-p`), `--tool` (`-t`), and `--json`.
- `jrc.py` (`jrc`): Interactive terminal REPL console with readline history, tab completion, multi-line input, and inline slash commands (`/claude`, `/agy`, `/auto`, `/ctx`, `/d`, `/p`, `/y`, `/m`, `/cd`, `/models`).
- `jrc_context.py`: Pure-Python, git-free shared memory engine that maintains `.jrc_context.md` across Claude Code and Antigravity sessions (task history, recent file changes via MD5 diffing, directory tree, and persistent architectural notes).
- `requirements.txt` & `package.json`: Python and Node dependencies (`@typesafe-ai/sdk`).

## Setup

1. Install Python dependencies in a virtual environment:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
2. Make entrypoints executable and symlink to `PATH`:
   ```bash
   chmod +x router.py jrc.py
   ln -sf "$(pwd)/router.py" ~/.local/bin/jroute
   ln -sf "$(pwd)/jrc.py" ~/.local/bin/jrc
   ```
3. Optional environment variables:
   - `TYPESAFE_BASE_URL` (default: `http://127.0.0.1:11435`)
   - `TYPESAFE_API_KEY` (default: `local`)
   - `TYPESAFE_DEFAULT_MODEL` (default: `largha`)

## Usage Examples

```bash
# Interactive console
jrc

# Direct routing
jroute "Fix the mobile layout issue on the checkout page"

# Preview routing decision without executing
jroute -d "commit push"
jroute --json "Analyze the PostgreSQL deadlock issue"

# Force a specific CLI tool while letting Jev pick sub-model and effort
jroute -t claude "Optimize the stock movement query in Odoo"
jroute -t agy "Design a new analytics dashboard page"
```
