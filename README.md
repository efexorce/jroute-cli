# jroute-cli — Intelligent CLI Meta-Router for Claude Code & Antigravity (`jroute` & `jrc`)

[![Agensi Marketplace](https://img.shields.io/badge/Agensi.io-jroute--cli%20(%247)-2563eb)](https://www.agensi.io/skills/jroute-cli)
[![Agensi Marketplace](https://img.shields.io/badge/Agensi.io-jroute--cli--manager%20(%241)-16a34a)](https://agensi.io/skills/jroute-cli-manager)

**jroute-cli** is a local System-1 decision router powered by the **TypeSafe AI Jev SDK** (`system_one`) and a local **Ollaya** server. It analyzes developer prompts alongside the active workspace context (`cwd`, project files, tech stack markers) in a single pass—evaluating task complexity (`Score`), production risk (`Noul`), and tool fit (`Choice`)—to automatically select and launch the optimal CLI agent (**Claude Code** or **Antigravity / `agy`**), sub-model, and reasoning effort level.

---

## Features

- **Single-Pass System-1 Routing**: Evaluates `route`, `tool`, `complexity`, and `is_critical` simultaneously via `client.system_one(...)` on a local Ollaya endpoint (`http://127.0.0.1:11435`).
- **Workspace-Aware Context**: Automatically detects stack markers (`python`, `node/js`, `rust`, `go`, `docker`, `odoo`, `git-repo`) and top-level project files before routing.
- **Dynamic Sub-Model & Effort Calibration**: Automatically scales reasoning effort (`low`, `medium`, `high`, `xhigh`) and upgrades sub-models when high complexity or production criticality is detected.
- **Two CLI Entrypoints**:
  - `jroute`: Fast single-command CLI router for direct execution, piping, `--dry-run`, and `--json` inspection.
  - `jrc`: Interactive terminal REPL console with live spinner, readline history, tab completion, multi-line input, and inline slash commands.
- **Git-Free Shared Memory Layer (`.jrc_context.md`)**:
  - Pure Python workspace state tracking without any `git` dependency or command calls.
  - Zero cloud token cost (100% local filesystem scanning, MD5 signatures, and metadata diffing).
  - Bridges context between **Claude Code** and **Antigravity (`agy`)** across sessions.
  - Automatically records task history, recent file modifications/additions/deletions, directory tree, and preserves persistent developer/agent architectural notes.

---

## Supported Route Profiles

| Profile Key | CLI Tool | Sub-Model | Default Effort | Target Use Cases |
| :--- | :--- | :--- | :--- | :--- |
| `claude_haiku` | `claude` | `claude-haiku-5-5` | `low` | Fast Git operations (`commit`, `push`, `branch`), small script tweaks, formatting, README edits |
| `claude_sonnet` | `claude` | `claude-sonnet-5-5` | `medium` / `high` | Standard software engineering, bug fixes, feature implementation, refactoring, unit tests |
| `claude_opus` | `claude` | `claude-opus-5-5` | `high` / `xhigh` | Complex multi-file refactoring, critical production debugging, database/ORM architecture (Odoo, PostgreSQL) |
| `agy_flash` | `agy` | `gemini-3.8-flash-low\|medium` | `low` | Quick lookups, status checks, log inspection, simple shell commands |
| `agy_argon` | `agy` | `gemini-4-argon-medium\|high` | `medium` / `high` | Full-stack web development, UI/UX design, API/webhook integration, bots, multi-step automation |
| `agy_hydrogen` | `agy` | `gemini-4-hydrogen-high\|xhigh` | `high` / `xhigh` | Deep research, multi-agent orchestration, complex algorithmic & system-wide design |

---

## Installation

1. **Install dependencies**:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

2. **Symlink `jroute` and `jrc` to your PATH**:
   ```bash
   chmod +x router.py jrc.py
   ln -sf "$(pwd)/router.py" ~/.local/bin/jroute
   ln -sf "$(pwd)/jrc.py" ~/.local/bin/jrc
   ```

3. **Environment Variables (Optional)**:
   - `TYPESAFE_BASE_URL`: Ollaya / Jev API endpoint (default: `http://127.0.0.1:11435`)
   - `TYPESAFE_API_KEY`: API key (default: `local`)
   - `TYPESAFE_DEFAULT_MODEL`: Decision model name (default: `largha`)

---

## Usage

### 1. Interactive Terminal Console (`jrc`)

Run `jrc` without arguments from any project directory to open the interactive console:

```bash
jrc
```

```text
╭──────────────────────────────────────────────────────────────────────╮
│ ⚡ Jev Router Konsolu (jrc) (Ollaya: largha)
│ 📂 Dizin  : /path/to/workspace [python]
│ 🎯 Hedef  : otomatik (claude / agy)   ⚙️  Mod : interaktif · skip-perms (-y)
│ 🧠 Hafıza : .jrc_context.md [3 görev · 2 son değişiklik]
│ 💡 Komut  : /claude · /agy · /auto · /ctx · /d · /p · /y · /m · /help · q / Ctrl+C
╰──────────────────────────────────────────────────────────────────────╯
jrc [auto·yes] ❯ 
```

#### Console Commands & Prefixes
- `<prompt>`: Analyzes the prompt with Jev and launches the selected CLI agent (`claude` or `agy`).
- `!d <prompt>` / `/d`: Dry-run mode — displays the routing decision, probability distribution, and generated CLI command without executing it.
- `!p <prompt>` / `/p`: One-shot print mode (`-p`) — runs the agent in non-interactive print mode and returns to the `jrc` prompt.
- `@claude <prompt>` / `@agy <prompt>`: Forces `claude` or `agy` for a single message while letting Jev pick the optimal sub-model and effort.
- `/claude`, `/agy`, `/auto`: Pins the target CLI tool for the session or resets to automatic routing.
- `/ctx`, `/context`: Inspects the shared memory layer (`.jrc_context.md`).
  - `/ctx`: Displays quick summary of recorded tasks, recent file changes, and architectural notes.
  - `/ctx note <text>`: Appends a persistent rule or architectural decision to the memory layer.
  - `/ctx sync`: Manually re-scans the filesystem and updates changes.
  - `/ctx cat`: Prints the full `.jrc_context.md` file.
- `/y` / `/yes`: Toggles `--dangerously-skip-permissions` (enabled by default).
- `/m` (or trailing `\`): Multi-line prompt input mode.
- `/cd <dir>`: Changes the active workspace directory inside the console and syncs its memory layer.
- `/models`: Lists all available routing profiles and criteria.
- `/help`, `/clear`, `q` / `exit` / `Ctrl+C`: Help, clear screen, or exit.

---

### 2. Direct Single-Command Router (`jroute`)

```bash
# Analyze the task and launch the chosen CLI agent interactively:
jroute "Fix the mobile layout issue on the checkout page"

# Run in one-shot (--print / -p) mode:
jroute -p "Summarize the errors in the last 20 lines of app.log"

# Force a specific CLI tool while letting Jev choose the sub-model and effort:
jroute -t claude "Optimize the stock movement query in Odoo"
jroute -t agy "Design a new analytics dashboard page"

# Preview the routing decision and generated command without running (--dry-run / -d):
jroute -d "commit push"

# Output full routing metrics and probability distributions as JSON:
jroute --json "Analyze the PostgreSQL deadlock issue"
```

## JRC Geçidi (tüm botlar/servisler jrc'den geçer)

> **Agensi Eklentisi:** Yönetici katmanı, merkezi geçit ve token defteri modülleri Agensi Marketplace'te [`jroute-cli-manager`](https://agensi.io/skills/jroute-cli-manager) ($1) olarak da paketlenmiştir.

Peribot (openclaw/Peri), geocubot, eretnabot ve Hermes `claude`/`agy` motorunu kendi seçemez.
Karar tek yerde verilir: `jrc_gate.decide(kaynak, görev)`.

- Politika: `jrc_policy.json` — kaynak başına `pin` (analizsiz sabit profil → 0 gecikme/0 token), `max_effort`, `allow_tools`; global `engine` (`auto|claude|agy`).
- Defter: `~/.cache/jroute/gate.jsonl` (karar, fallback, sonuç).
- Komutlar: `jrc gate [saat]` · `jrc audit` · `jrc engine auto|claude|agy` · `jrc policy`.
- Bağlı noktalar (odoo): `maliyet_defteri.claude_calistir`, `argon_gemini_proxy.py` (Hermes+Peri), `hizli_yol.py`.
- Kota fallback'ini de servis değil jrc yapar (`jrc_gate.fallback`); motor kilitliyse sessiz geçiş yapılmaz.

## Yönetici Katmanı (Fable işi yönetir)

jrc motoru, modeli ve eforu seçtikten sonra işin **nasıl** yapılacağını Fable belirler (`jrc_director.py`):

1. Fable salt-okunur araçlarla (`Read,Grep,Glob`) projeye ve `.jrc_context.md`'ye bakıp yürütücü için bir iş emri yazar.
2. Seçilen yürütücü (claude/agy) iş emriyle çalışır; `.jrc_context.md`'ye yazmaz, raporunu `~/.cache/jroute/reports/` altına bırakır.
3. Fable raporu ve değişen dosyaları denetler, `.jrc_context.md` → "Ortak Notlar" satırını yazar.

- Yürütücü zaten Fable ise ek çağrı yapılmaz; Fable hem yönetir hem notu kendi yazar.
- Fable'a ulaşılamazsa görev eski (yönetimsiz) akışla çalışır. `-d` / `--json` Fable'ı çağırmaz.
- Yalnızca `jrc` / `jroute` görevlerinde geçerlidir; `jrc_gate.decide()` kullanan botlar/servisler kapsam dışıdır.
- Aç/kapat: `jrc director on|off`. Ayar: `jrc_policy.json` → `"director": {"enabled", "model", "effort", "timeout_s"}`. Çağrılar geçit defterine `director` kaynağıyla yazılır.

## Token defteri (`jrc tokens`)

`jrc_tokens.py` motor ve model bazında token yakımını kalıcı olarak kaydeder:

- **Kaynak:** Claude Code oturum kayıtlarındaki gerçek `usage` sayaçları (`~/.claude/projects/**/*.jsonl`, alt ajanlar dahil) + maliyet defterindeki agy ve doğrudan API çağrıları.
- **Kalıcı defter:** `~/.cache/jroute/tokens.db` (SQLite). Claude oturum dosyalarını ~30 günde siler; defter silmez.
- **Güncelleme:** cron 5 dakikada bir (`jrc_tokens.py --sessiz`) ve her jrc görevinden sonra arka planda.
- **Pano:** özet `odoo/addons/.jrc_tokens.json` dosyasına yazılır; `eretnastore.com/yonetim#hermes-token` (Hermes → 🔥 Token) okur.
- **Komut:** `jrc tokens` (terminal raporu), `jrc tokens --json`.
- "API eşdeğeri $" liste fiyatıyla hesaptır (`PRICES` tablosu); abonelikte ödenmez.
