#!/usr/bin/env python3
"""
JRC Ortak Hafıza Katmanı (jrc_context.py)

Git bağımsız, saf Python tabanlı ve sıfır cloud token maliyetli ortak hafıza modülü.
Claude Code ve Antigravity (agy) arasında proje bağlamı, görev geçmişi, dosya değişiklikleri
ve mimari kararları `.jrc_context.md` ve `.jrc_state.json` üzerinden senkronize eder.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

CONTEXT_FILENAME = ".jrc_context.md"
STATE_FILENAME = ".jrc_state.json"
MAX_HISTORY_ENTRIES = 12
MAX_FILES_TO_SCAN = 800
MAX_SCAN_DEPTH = 6

IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".cache",
    ".idea",
    ".vscode",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    "bin",
    "obj",
    ".next",
    ".nuxt",
    ".output",
    "coverage",
}

IGNORE_FILES = {
    CONTEXT_FILENAME,
    STATE_FILENAME,
    ".DS_Store",
}

IGNORE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".lock",
    ".log",
    ".tmp",
    ".swp",
}

USER_NOTES_START = "<!-- USER_NOTES_START -->"
USER_NOTES_END = "<!-- USER_NOTES_END -->"

CACHE_DIR = Path.home() / ".cache" / "jroute"
WEB_JOBS_DIR = CACHE_DIR / "web_jobs"
EMIR_SEQ_FILE = CACHE_DIR / "emir_seq.json"
EMIR_REF_RE = re.compile(r"\bem(?:i|İ|ı)r\s*(\d+)\b", re.IGNORECASE)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")
EMIR_LOG_MAX_LINES = 60
EMIR_CONTEXT_MAX_CHARS = 4000


def extract_emir_numbers(task: str) -> list[int]:
    """Görev metnindeki `emirN` referanslarını sırasını koruyarak tekilleştirilmiş olarak döndürür."""
    if not task or task.lstrip().startswith("[Önceki emir"):
        return []
    nums: list[int] = []
    seen: set[int] = set()
    for m in EMIR_REF_RE.finditer(task):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n not in seen:
            seen.add(n)
            nums.append(n)
    return nums


def find_emir_by_no(no: int) -> tuple[dict[str, Any] | None, Path | None]:
    """Verilen emir numarasına ait web_jobs meta sözlüğünü ve log yolunu bulur."""
    if not WEB_JOBS_DIR.exists():
        return None, None

    # 1. Hızlı arama: emir_seq.json içindeki job_id -> no eşleşmesi
    if EMIR_SEQ_FILE.exists():
        try:
            seq_data = json.loads(EMIR_SEQ_FILE.read_text(encoding="utf-8"))
            if isinstance(seq_data, dict):
                assigned = seq_data.get("assigned")
                if isinstance(assigned, dict):
                    for jid, assigned_no in assigned.items():
                        if assigned_no == no:
                            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(jid))
                            meta_path = WEB_JOBS_DIR / f"{safe_id}.json"
                            if meta_path.exists():
                                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                                if isinstance(meta, dict):
                                    meta.setdefault("no", no)
                                    return meta, WEB_JOBS_DIR / f"{safe_id}.log"
        except (OSError, ValueError):
            pass

    # 2. Yedek arama: tüm meta dosyalarını tara
    for meta_path in WEB_JOBS_DIR.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("no") == no:
            return meta, WEB_JOBS_DIR / f"{meta_path.stem}.log"

    return None, None


def build_emir_context_block(task: str) -> str:
    """Görevdeki `emirN` referansları için yürütücüye eklenecek bağlam bloğunu üretir."""
    nums = extract_emir_numbers(task)
    if not nums:
        return ""

    # Toplam eklentiyi ~4000 karakter altında tutacak şekilde log bütçesi ayarla
    per_log_budget = max(350, (EMIR_CONTEXT_MAX_CHARS - 320 * len(nums)) // len(nums))
    blocks: list[str] = []

    for no in nums:
        meta, log_path = find_emir_by_no(no)
        if meta is None:
            blocks.append(f"[Önceki emir{no}: emir{no} bulunamadı]")
            continue

        prev_task = str(meta.get("task") or "").strip()
        if len(prev_task) > 360:
            prev_task = prev_task[:360] + "…"

        status = str(meta.get("status") or "unknown")
        if meta.get("dry_run"):
            status += " (dry-run)"
        if meta.get("exit_code") is not None:
            status += f", çıkış={meta['exit_code']}"

        dec = meta.get("decision") if isinstance(meta.get("decision"), dict) else {}
        if dec and dec.get("tool"):
            tool_model = f"{dec.get('tool')}/{dec.get('model', '-')}"
        else:
            tool_model = str(meta.get("tool") or "auto")

        finished = str(
            meta.get("finished_at")
            or meta.get("started_at")
            or meta.get("created_at")
            or "-"
        )

        log_tail = "(log çıktısı yok)"
        if log_path is not None and log_path.exists():
            try:
                raw_log = log_path.read_bytes().decode("utf-8", errors="replace")
                clean_log = ANSI_ESCAPE_RE.sub("", raw_log).strip()
                if clean_log:
                    lines = clean_log.splitlines()
                    tail_str = "\n".join(lines[-EMIR_LOG_MAX_LINES:]).strip()
                    if len(tail_str) > per_log_budget:
                        tail_str = "…(kısaltıldı)\n" + tail_str[-(per_log_budget - 15):]
                    log_tail = tail_str
                else:
                    log_tail = "(log çıktısı boş)"
            except OSError:
                log_tail = "(log okunamadı)"

        blocks.append(
            f"[Önceki emir{no}: durum={status} | araç/model={tool_model} | bitiş={finished}]\n"
            f"Görev: {prev_task}\n"
            f"Son çıktı (~{EMIR_LOG_MAX_LINES} satır):\n"
            f"{log_tail}\n"
            f"[/Önceki emir{no}]"
        )

    joined = "\n\n".join(blocks)
    if len(joined) > EMIR_CONTEXT_MAX_CHARS:
        joined = joined[: EMIR_CONTEXT_MAX_CHARS - 20].rstrip() + "\n…(kısaltıldı)]"
    return joined


def resolve_emir_refs(task: str) -> str:
    """Görev metninde `emirN` referansı varsa ilgili geçmiş emir bağlamını başa ekler."""
    if not task or task.lstrip().startswith("[Önceki emir"):
        return task
    ctx_block = build_emir_context_block(task)
    if not ctx_block:
        return task
    return f"{ctx_block}\n\n{task}"


def scan_project_files(cwd: Path) -> dict[str, dict[str, Any]]:
    """
    Çalışma klasöründeki proje dosyalarını tarar.
    Git'e hiç dokunmadan dosya boyutları, mtime ve md5 hash haritası üretir.
    """
    files_map: dict[str, dict[str, Any]] = {}
    count = 0

    for root, dirs, files in os.walk(cwd):
        rel_root = os.path.relpath(root, cwd)
        depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        if depth >= MAX_SCAN_DEPTH:
            dirs.clear()
            continue

        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in IGNORE_DIRS]

        for file_name in files:
            if file_name in IGNORE_FILES or file_name.startswith("."):
                continue

            ext = os.path.splitext(file_name)[1].lower()
            if ext in IGNORE_EXTENSIONS:
                continue

            full_path = os.path.join(root, file_name)
            rel_path = os.path.relpath(full_path, cwd)
            try:
                stat = os.stat(full_path)
                size = stat.st_size
                mtime = stat.st_mtime

                # 2 MB altı dosyalar için hızlı md5 hash, büyük dosyalar için boyut+mtime imzası
                if size <= 2 * 1024 * 1024:
                    try:
                        with open(full_path, "rb") as f:
                            file_hash = hashlib.md5(f.read(), usedforsecurity=False).hexdigest()
                    except Exception:
                        file_hash = f"{size}_{int(mtime)}"
                else:
                    file_hash = f"{size}_{int(mtime)}"

                files_map[rel_path] = {
                    "mtime": mtime,
                    "size": size,
                    "hash": file_hash,
                }
                count += 1
                if count >= MAX_FILES_TO_SCAN:
                    break
            except Exception:
                continue

        if count >= MAX_FILES_TO_SCAN:
            break

    return files_map


def detect_file_changes(
    old_files: dict[str, dict[str, Any]],
    current_files: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    """Eski ve yeni dosya haritalarını karşılaştırarak eklenen, değişen ve silinenleri bulur."""
    old_set = set(old_files.keys())
    curr_set = set(current_files.keys())

    added = sorted(curr_set - old_set)
    deleted = sorted(old_set - curr_set)
    modified: list[str] = []

    for path in sorted(curr_set & old_set):
        old_info = old_files[path]
        curr_info = current_files[path]
        if old_info.get("hash") != curr_info.get("hash") or old_info.get("size") != curr_info.get("size"):
            modified.append(path)

    return {
        "added": added,
        "modified": modified,
        "deleted": deleted,
    }


def get_recently_modified_files(
    files_map: dict[str, dict[str, Any]],
    within_hours: float = 24.0,
) -> list[tuple[str, float, int]]:
    """Son X saat içinde değiştirilmiş dosyaları zaman sırasına göre döndürür."""
    now = datetime.datetime.now().timestamp()
    threshold = now - (within_hours * 3600)
    recent = []
    for path, info in files_map.items():
        mtime = info.get("mtime", 0.0)
        if mtime >= threshold:
            recent.append((path, mtime, info.get("size", 0)))
    recent.sort(key=lambda x: x[1], reverse=True)
    return recent


def build_project_tree(cwd: Path, max_entries: int = 14) -> str:
    """Projenin üst seviye klasör ve önemli dosyalarından oluşan hafif ASCII ağaç görünümü üretir."""
    lines: list[str] = []
    try:
        entries = sorted(
            [
                p for p in cwd.iterdir()
                if not p.name.startswith(".") and p.name not in IGNORE_DIRS
            ],
            key=lambda x: (not x.is_dir(), x.name.lower()),
        )
    except Exception:
        return "(dizin okunamadı)"

    shown = entries[:max_entries]
    for idx, item in enumerate(shown):
        is_last = idx == len(shown) - 1 and len(entries) <= max_entries
        prefix = "└── " if is_last else "├── "
        suffix = "/" if item.is_dir() else ""
        lines.append(f"{prefix}{item.name}{suffix}")

    if len(entries) > max_entries:
        lines.append(f"└── ... ({len(entries) - max_entries} ek öğe)")

    return "\n".join(lines) if lines else "(boş proje dizini)"


def extract_user_notes(content: str) -> str:
    """Mevcut .jrc_context.md dosyasındaki kullanıcı ve ajan notlarını ayıklar."""
    if USER_NOTES_START in content and USER_NOTES_END in content:
        start_idx = content.find(USER_NOTES_START) + len(USER_NOTES_START)
        end_idx = content.find(USER_NOTES_END)
        notes = content[start_idx:end_idx].strip()
        return notes
    return ""


def load_state(cwd: Path) -> dict[str, Any]:
    """Çalışma klasöründeki .jrc_state.json dosyasını okur."""
    state_file = cwd / STATE_FILENAME
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass

    return {
        "version": "1.0",
        "project_name": cwd.name,
        "created_at": datetime.datetime.now().isoformat(),
        "last_updated": datetime.datetime.now().isoformat(),
        "task_history": [],
        "file_snapshot": {},
        "recent_changes": {"added": [], "modified": [], "deleted": []},
    }


def save_state(cwd: Path, state: dict[str, Any]) -> None:
    """Çalışma klasöründeki .jrc_state.json dosyasını yazar."""
    state_file = cwd / STATE_FILENAME
    state["last_updated"] = datetime.datetime.now().isoformat()
    try:
        tmp_file = state_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp_file.replace(state_file)
    except Exception:
        pass


def detect_tech_stack(cwd: Path) -> list[str]:
    """Git komutlarına başvurmadan proje dizinindeki teknoloji yığınını tespit eder."""
    stack = []
    markers = [
        ("package.json", "node/js"),
        ("pyproject.toml", "python"),
        ("requirements.txt", "python"),
        ("Cargo.toml", "rust"),
        ("go.mod", "go"),
        ("pom.xml", "java/maven"),
        ("build.gradle", "java/gradle"),
        ("composer.json", "php"),
        ("docker-compose.yml", "docker"),
        ("Dockerfile", "docker"),
        ("odoo.conf", "odoo"),
    ]
    for name, label in markers:
        if (cwd / name).exists() and label not in stack:
            stack.append(label)
    return stack if stack else ["genel"]


def generate_context_markdown(
    cwd: Path,
    state: dict[str, Any],
    current_files: dict[str, dict[str, Any]],
    user_notes: str,
) -> str:
    """Git'e ihtiyaç duymayan, Claude Code ve Antigravity için temiz .jrc_context.md üretir."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    project_name = cwd.name
    stack_list = detect_tech_stack(cwd)
    stack_str = ", ".join(f"`{s}`" for s in stack_list)

    # Son görevler tablosu
    task_history = state.get("task_history", [])[:MAX_HISTORY_ENTRIES]
    if task_history:
        history_rows = []
        for t in task_history:
            raw_no = t.get("no")
            if isinstance(raw_no, int) and raw_no > 0:
                no_str = f"`emir{raw_no}`"
            elif isinstance(raw_no, str) and raw_no.strip():
                no_str = raw_no.strip()
            else:
                no_str = "-"
            timestamp = t.get("timestamp", "-")
            tool_name = t.get("tool", "-")
            model_name = t.get("model", "-")
            effort = t.get("effort", "-")
            raw_task = t.get("task", "").replace("\n", " ").replace("|", "/")
            short_task = (raw_task[:65] + "…") if len(raw_task) > 65 else raw_task
            history_rows.append(
                f"| {no_str} | {timestamp} | `{tool_name}` ({model_name}) | `{effort}` | {short_task} |"
            )
        history_table = (
            "| # | Zaman | Araç & Model | Efor | Görev / Prompt |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n" + "\n".join(history_rows)
        )
    else:
        history_table = "_Henüz kayıtlı görev geçmişi yok._"

    # Değişiklikler ve dosya durumu
    changes = state.get("recent_changes", {"added": [], "modified": [], "deleted": []})
    added = changes.get("added", [])
    modified = changes.get("modified", [])
    deleted = changes.get("deleted", [])

    changes_md_parts = []
    if modified:
        changes_md_parts.append(f"- **Değiştirilen Dosyalar ({len(modified)})**: " + ", ".join(f"`{p}`" for p in modified[:10]))
    if added:
        changes_md_parts.append(f"- **Yeni Eklenen Dosyalar ({len(added)})**: " + ", ".join(f"`{p}`" for p in added[:10]))
    if deleted:
        changes_md_parts.append(f"- **Silinen Dosyalar ({len(deleted)})**: " + ", ".join(f"`{p}`" for p in deleted[:10]))

    if not changes_md_parts:
        # Değişiklik listesi boşsa son 24 saatte değişen dosyaları listele
        recent_m = get_recently_modified_files(current_files, within_hours=24.0)
        if recent_m:
            items = [f"`{p[0]}`" for p in recent_m[:8]]
            changes_md = "- **Son 24 Saatte Değişenler**: " + ", ".join(items)
        else:
            changes_md = "- _Tüm dosyalar güncel, bekleyen değişiklik yok._"
    else:
        changes_md = "\n".join(changes_md_parts)

    total_files = len(current_files)
    tree_str = build_project_tree(cwd)

    notes_body = user_notes.strip() if user_notes.strip() else (
        "- Bu alana kullanıcı veya ajanlar (Claude Code / Antigravity) kalıcı mimari kararları, "
        "proje kurallarını veya yapılacakları not düşebilir. Otomatik güncellemelerde bu bölüm aynen korunur."
    )

    last_agent_info = ""
    if task_history:
        latest = task_history[0]
        last_agent_info = f"- **Son Çalışan Ajan**: `{latest.get('tool', '-')}` ({latest.get('model', '-')}) · efor: `{latest.get('effort', '-')}`\n"

    md = f"""# 🧠 JRC Ortak Hafıza & Proje Bağlamı (`.jrc_context.md`)
> **Otomatik Yönetilen Katman**: Bu dosya Jev Router (`jroute` / `jrc`) tarafından saf Python ile yönetilir.
> **Git Bağımsız**: Projede Git kullanılmamaktadır. **Claude Code** ve **Antigravity (agy)** bu hafıza dosyasını ortak bağlam olarak kullanır.
> **Maliyet**: Sıfır cloud token (yerel dosya tabanlı).

---

## 📌 Proje Özeti
- **Proje Adı**: `{project_name}`
- **Dizin**: `{cwd}`
- **Teknoloji Yığını**: {stack_str}
- **Kayıtlı Dosya Sayısı**: {total_files} adet
- **Son Hafıza Güncellemesi**: {now_str}
{last_agent_info}
---

## 🕒 Son Görevler & Ajan Geçmişi
{history_table}

---

## 📁 Dosya Durumu & Son Değişiklikler (Git-free)
{changes_md}

---

## 🏗️ Proje Mimarisi & Önemli Dosyalar
```text
{tree_str}
```

---

## 📝 Ortak Notlar & Mimari Kararlar (Kalıcı Alan)
{USER_NOTES_START}
{notes_body}
{USER_NOTES_END}
"""
    return md


def sync_workspace_memory(
    cwd: Path,
    new_task_entry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """
    Çalışma klasöründeki hafızayı senkronize eder:
    1. Mevcut dosya haritasını tarar (Git kullanmadan).
    2. Önceki durumla karşılaştırıp dosya değişikliklerini bulur.
    3. Varsa yeni görevi görev geçmişine ekler.
    4. .jrc_context.md içerisindeki kullanıcı notlarını korur.
    5. .jrc_context.md ve .jrc_state.json dosyalarını atomik olarak günceller.
    """
    context_path = cwd / CONTEXT_FILENAME
    state = load_state(cwd)

    current_files = scan_project_files(cwd)
    old_files = state.get("file_snapshot", {})

    changes = detect_file_changes(old_files, current_files)

    if new_task_entry:
        history = state.get("task_history", [])
        history.insert(0, new_task_entry)
        state["task_history"] = history[:MAX_HISTORY_ENTRIES]
        state["last_agent"] = new_task_entry.get("tool", "")
        state["last_model"] = new_task_entry.get("model", "")

    state["recent_changes"] = changes
    state["file_snapshot"] = current_files

    # Kullanıcı notlarını mevcut .jrc_context.md dosyasından oku
    user_notes = ""
    if context_path.exists():
        try:
            with open(context_path, "r", encoding="utf-8") as f:
                user_notes = extract_user_notes(f.read())
        except Exception:
            user_notes = ""

    md_content = generate_context_markdown(
        cwd=cwd,
        state=state,
        current_files=current_files,
        user_notes=user_notes,
    )

    # .jrc_context.md dosyasını yaz
    try:
        tmp_ctx = context_path.with_suffix(".tmp")
        with open(tmp_ctx, "w", encoding="utf-8") as f:
            f.write(md_content)
        tmp_ctx.replace(context_path)
    except Exception as e:
        sys.stderr.write(f"Hafıza dosyası yazılamadı: {e}\n")

    save_state(cwd, state)
    return state, context_path


def record_task_and_sync(
    cwd: Path,
    task: str,
    decision: dict[str, Any],
    no: int | None = None,
) -> Path:
    """Görev çalıştırma öncesinde görevi hafızaya ekler ve dosyayı günceller."""
    now_str = datetime.datetime.now().strftime("%d.%m %H:%M")
    entry: dict[str, Any] = {
        "timestamp": now_str,
        "tool": decision.get("tool", "unknown"),
        "model": decision.get("model", "unknown"),
        "effort": decision.get("effort", "medium"),
        "task": task.strip(),
    }
    if no is not None:
        entry["no"] = int(no)
    _, context_path = sync_workspace_memory(cwd, new_task_entry=entry)
    return context_path


def get_workspace_memory_summary(cwd: Path) -> dict[str, Any]:
    """
    Router ve konsol başlığı için hızlı hafıza özeti üretir (sıfır gecikme, salt-okunur).
    """
    context_path = cwd / CONTEXT_FILENAME
    state_path = cwd / STATE_FILENAME

    if not context_path.exists() and not state_path.exists():
        return {
            "has_memory": False,
            "task_count": 0,
            "recent_tasks": [],
            "modified_count": 0,
            "recent_modified_files": [],
            "last_agent": "",
            "last_updated": "",
        }

    state = load_state(cwd)
    history = state.get("task_history", [])
    changes = state.get("recent_changes", {})

    modified_count = (
        len(changes.get("modified", []))
        + len(changes.get("added", []))
        + len(changes.get("deleted", []))
    )

    recent_tasks = [t.get("task", "") for t in history[:3]]

    return {
        "has_memory": True,
        "task_count": len(history),
        "recent_tasks": recent_tasks,
        "modified_count": modified_count,
        "recent_modified_files": changes.get("modified", [])[:5],
        "last_agent": state.get("last_agent", ""),
        "last_updated": state.get("last_updated", ""),
    }


def append_user_note(cwd: Path, note_text: str) -> bool:
    """Kullanıcının veya ajanın hafıza dosyasına kalıcı not eklemesini sağlar."""
    context_path = cwd / CONTEXT_FILENAME
    if not context_path.exists():
        sync_workspace_memory(cwd)

    try:
        with open(context_path, "r", encoding="utf-8") as f:
            content = f.read()

        current_notes = extract_user_notes(content)
        today = datetime.datetime.now().strftime("%d.%m %H:%M")
        new_note_line = f"- [{today}] {note_text.strip()}"
        if current_notes:
            updated_notes = current_notes + "\n" + new_note_line
        else:
            updated_notes = new_note_line

        state = load_state(cwd)
        current_files = state.get("file_snapshot", {})
        if not current_files:
            current_files = scan_project_files(cwd)

        new_md = generate_context_markdown(cwd, state, current_files, updated_notes)
        with open(context_path, "w", encoding="utf-8") as f:
            f.write(new_md)
        return True
    except Exception:
        return False
