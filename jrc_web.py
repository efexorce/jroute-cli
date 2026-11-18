#!/home/efe/Desktop/jev/.venv/bin/python3
"""
JRC Web Arayüzü (jrc web)

Yerel (yalnızca 127.0.0.1) çalışan, sıfır harici bağımlılıklı (stdlib http.server)
Jev Router / JRC web konsolu. Görevleri konsoldaki `jrc` ile birebir aynı akıştan
(analiz → gate.jsonl → .jrc_context.md → jrc_director → yürütücü → token yenileme)
geçirerek arka plan alt sürecinde çalıştırır ve çıktıyı sayfaya canlı aktarır.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import jrc  # noqa: E402
import jrc_context  # noqa: E402
import jrc_director  # noqa: E402
import jrc_gate  # noqa: E402
import router  # noqa: E402
import stat as _stat  # noqa: E402


def _safe_scan_project_files(cwd: Path) -> dict[str, dict[str, Any]]:
    """FIFO/socket dosyalarında (ör. /tmp içinde) bloklanmayan güvenli dosya tarayıcı."""
    resolved = cwd.resolve()
    if resolved == Path("/tmp"):
        return {}
    files_map: dict[str, dict[str, Any]] = {}
    count = 0
    for root, dirs, files in os.walk(cwd):
        rel_root = os.path.relpath(root, cwd)
        depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        if depth >= jrc_context.MAX_SCAN_DEPTH:
            dirs.clear()
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in jrc_context.IGNORE_DIRS]
        for file_name in files:
            if file_name in jrc_context.IGNORE_FILES or file_name.startswith("."):
                continue
            ext = os.path.splitext(file_name)[1].lower()
            if ext in jrc_context.IGNORE_EXTENSIONS:
                continue
            full_path = os.path.join(root, file_name)
            rel_path = os.path.relpath(full_path, cwd)
            try:
                st = os.lstat(full_path)
                if not _stat.S_ISREG(st.st_mode):
                    continue
                size = st.st_size
                mtime = st.st_mtime
                file_hash = f"{size}_{int(mtime)}"
                files_map[rel_path] = {"mtime": mtime, "size": size, "hash": file_hash}
                count += 1
                if count >= jrc_context.MAX_FILES_TO_SCAN:
                    break
            except Exception:
                continue
        if count >= jrc_context.MAX_FILES_TO_SCAN:
            break
    return files_map


jrc_context.scan_project_files = _safe_scan_project_files

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_CWD = str(Path.home() / "Desktop" / "jev" / "projeler")

CACHE_DIR = Path.home() / ".cache" / "jroute"
TOKEN_FILE = CACHE_DIR / "web_token"
WEB_JOBS_DIR = CACHE_DIR / "web_jobs"
STATE_FILE = CACHE_DIR / "web_state.json"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def ensure_token() -> str:
    """~/.cache/jroute/web_token dosyasını 0600 izinle üretir veya okur."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        try:
            tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if tok:
                os.chmod(TOKEN_FILE, 0o600)
                return tok
        except OSError:
            pass
    tok = secrets.token_urlsafe(24)
    fd = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return tok


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def get_last_cwd() -> str:
    cwd = load_state().get("last_cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    return DEFAULT_CWD


def set_last_cwd(cwd: str) -> None:
    st = load_state()
    st["last_cwd"] = cwd
    save_state(st)


def _job_meta_path(job_id: str) -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", job_id)
    return WEB_JOBS_DIR / f"{safe_id}.json"


def _job_log_path(job_id: str) -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", job_id)
    return WEB_JOBS_DIR / f"{safe_id}.log"


def _save_job_meta(job_id: str, meta: dict[str, Any]) -> None:
    WEB_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    p = _job_meta_path(job_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def _load_job_meta(job_id: str) -> dict[str, Any] | None:
    p = _job_meta_path(job_id)
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if meta.get("status") == "running" and meta.get("pid"):
        try:
            os.kill(int(meta["pid"]), 0)
        except ProcessLookupError:
            # Alt süreç beklenmedik şekilde kapandıysa durumu güncelle
            meta["status"] = "error"
            meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_job_meta(job_id, meta)
        except PermissionError:
            pass
    return meta


def list_jobs(limit: int = 25) -> list[dict[str, Any]]:
    WEB_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(WEB_JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    items: list[dict[str, Any]] = []
    for f in files:
        meta = _load_job_meta(f.stem)
        if meta:
            items.append(meta)
    return items


# ── Görev kuyruğu (FIFO): aynı anda tek ajan görevi çalışır ─────────────────
_QUEUE_COND = threading.Condition()
_PROCS: dict[str, subprocess.Popen] = {}


def _all_job_metas() -> list[dict[str, Any]]:
    WEB_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for f in WEB_JOBS_DIR.glob("*.json"):
        meta = _load_job_meta(f.stem)
        if meta:
            out.append(meta)
    return out


def _queued_jobs() -> list[dict[str, Any]]:
    """Bekleyen işler, sıraya giriş zamanına göre (eskiden yeniye)."""
    q = [m for m in _all_job_metas() if m.get("status") == "queued"]
    q.sort(key=lambda m: (m.get("queued_at") or m.get("created_at") or "", m.get("id") or ""))
    return q


def _queue_positions() -> dict[str, int]:
    return {m["id"]: i + 1 for i, m in enumerate(_queued_jobs())}


def _reap_procs() -> None:
    # Bitmiş alt süreçleri topla (zombi kalmasın, os.kill(pid,0) yanıltmasın)
    for jid, proc in list(_PROCS.items()):
        if proc.poll() is not None:
            _PROCS.pop(jid, None)
            meta = _load_job_meta(jid)
            if meta and meta.get("status") == "running":
                # İşçi durumu yazamadan öldüyse
                meta["status"] = "error"
                meta["exit_code"] = proc.returncode
                meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save_job_meta(jid, meta)


def _launch_job(meta: dict[str, Any]) -> None:
    job_id = meta["id"]
    log_path = _job_log_path(job_id)
    meta["status"] = "running"
    meta["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_job_meta(job_id, meta)
    with log_path.open("ab") as log_f:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--worker", job_id],
            cwd=str(meta["cwd"]),
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    if not meta.get("dry_run"):
        _PROCS[job_id] = proc

    def _wait_and_kick() -> None:
        proc.wait()
        _kick_queue()  # iş biter bitmez sıradakini başlat

    threading.Thread(target=_wait_and_kick, daemon=True).start()
    meta["pid"] = proc.pid
    _save_job_meta(job_id, meta)


def _dispatch_once() -> None:
    """Çalışan (dry-run olmayan) iş yoksa sıradaki ilk işi başlatır."""
    _reap_procs()
    metas = _all_job_metas()
    busy = any(m.get("status") == "running" and not m.get("dry_run") for m in metas)
    if busy:
        return
    queued = [m for m in metas if m.get("status") == "queued"]
    if not queued:
        return
    queued.sort(key=lambda m: (m.get("queued_at") or m.get("created_at") or "", m.get("id") or ""))
    nxt = queued[0]
    if not Path(str(nxt.get("cwd") or "")).is_dir():
        nxt["status"] = "error"
        nxt["exit_code"] = 1
        nxt["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_job_meta(nxt["id"], nxt)
        _job_log_path(nxt["id"]).open("a", encoding="utf-8").write(
            f"✗ Çalışma dizini bulunamadı: {nxt.get('cwd')}\n"
        )
        return
    _launch_job(nxt)


def _dispatcher_loop() -> None:
    while True:
        with _QUEUE_COND:
            try:
                _dispatch_once()
            except Exception as exc:  # dağıtıcı asla ölmemeli
                print(f"Kuyruk dağıtıcı hatası: {exc}", file=sys.stderr, flush=True)
            _QUEUE_COND.wait(timeout=1.5)


def _kick_queue() -> None:
    with _QUEUE_COND:
        _QUEUE_COND.notify_all()


def cancel_queued(job_id: str) -> tuple[bool, str]:
    with _QUEUE_COND:
        meta = _load_job_meta(job_id)
        if not meta:
            return False, f"İş bulunamadı: {job_id}"
        if meta.get("status") != "queued":
            return False, "Yalnızca sıradaki (başlamamış) işler iptal edilebilir."
        meta["status"] = "cancelled"
        meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_job_meta(job_id, meta)
        _job_log_path(job_id).open("a", encoding="utf-8").write("⊘ Sıradan çıkarıldı (iptal).\n")
    return True, "İptal edildi."


def run_worker(job_id: str) -> int:
    """Arka plan görev işçisi: jrc konsolunun run_agent akışıyla birebir aynı yoldan yürütür."""
    meta = _load_job_meta(job_id)
    if not meta:
        print(f"✗ İş kaydı bulunamadı: {job_id}", file=sys.stderr, flush=True)
        return 1

    task = str(meta.get("task") or "").strip()
    target_cwd = Path(os.path.expanduser(str(meta.get("cwd") or DEFAULT_CWD))).resolve()
    raw_tool = str(meta.get("tool") or "").strip().lower()
    forced_tool = raw_tool if raw_tool in ("claude", "agy") else None
    dry_run = bool(meta.get("dry_run", False))

    try:
        print("⚡ Jev görev analizi yapılıyor...", flush=True)
        decision = router.analyze_task_with_jev(
            task=task,
            cwd=target_cwd,
            forced_tool=forced_tool,
        )
        cmd = router.build_cli_command(
            decision=decision,
            task=task,
            print_mode=True,
            skip_permissions=True,
        )
        meta["decision"] = decision
        meta["command"] = cmd
        _save_job_meta(job_id, meta)

        m = decision.get("metrics", {})
        director_active = jrc_director.active_for(decision)
        director_str = (
            f"açık ({jrc_director.director_name()})"
            if director_active
            else "atlandı / doğrudan yürütme"
        )
        cmd_preview = " ".join(
            repr(a[:60] + ("…" if len(a) > 60 else "")) if " " in a else a for a in cmd
        )
        print(
            f"⚡ Jev Router [{decision.get('decision_model', '')} · {decision.get('latency_ms', 0)} ms]\n"
            f"  ├─ Araç & Model  : {decision['tool']} → {decision['model']} ({decision.get('label', '')})\n"
            f"  ├─ Efor & Risk   : effort={decision['effort']} | "
            f"karmaşıklık={m.get('complexity_level', '?')} ({m.get('complexity_score', 0)}) | "
            f"kritiklik={m.get('critical_probability', 0)}\n"
            f"  ├─ Yönetici      : {director_str}\n"
            f"  ├─ Çalışma Dizini: {target_cwd}\n"
            f"  └─ Komut         : {cmd_preview}\n",
            flush=True,
        )

        if dry_run:
            print("✓ Dry-run tamamlandı (komut çalıştırılmadı).", flush=True)
            meta["status"] = "done"
            meta["exit_code"] = 0
            meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_job_meta(job_id, meta)
            return 0

        # Görev öncesi .jrc_context.md ortak hafıza ve gate.jsonl defter kaydı
        try:
            jrc_context.record_task_and_sync(target_cwd, task, decision)
            jrc_gate.record("jrc", decision, task)
        except Exception as exc:
            print(f"Hafıza/geçit güncellenirken uyarı: {exc}", file=sys.stderr, flush=True)

        os.chdir(target_cwd)

        if director_active:
            rc = jrc_director.run(
                task,
                decision,
                target_cwd,
                True,
                lambda brief: router.build_cli_command(
                    decision=decision,
                    task=task,
                    print_mode=True,
                    skip_permissions=True,
                    brief=brief,
                ),
            )
        else:
            rc = subprocess.run(cmd, cwd=str(target_cwd), stdin=subprocess.DEVNULL).returncode

        jrc._refresh_tokens_bg()
        try:
            jrc_context.sync_workspace_memory(target_cwd)
            print("\n✓ Ajan oturumu tamamlandı. .jrc_context.md hafızası güncellendi.", flush=True)
        except Exception:
            pass

        meta["status"] = "done" if rc == 0 else "error"
        meta["exit_code"] = int(rc)
        meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_job_meta(job_id, meta)
        return int(rc)

    except Exception as exc:
        print(f"\n✗ Görev yürütme hatası: {exc}", file=sys.stderr, flush=True)
        meta["status"] = "error"
        meta["exit_code"] = 1
        meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_job_meta(job_id, meta)
        return 1


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>JRC Web Konsolu — Jev Router</title>
  <style>
    :root {
      --bg: #0d1117;
      --panel: #161b22;
      --panel-alt: #1c2128;
      --border: #30363d;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #58a6ff;
      --green: #3fb950;
      --yellow: #d29922;
      --red: #f85149;
      --purple: #bc8cff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    .wrap {
      max-width: none;
      margin: 0 auto;
      padding: 20px 16px 48px;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 18px;
    }
    header h1 {
      margin: 0;
      font-size: 20px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .sub {
      color: var(--muted);
      font-size: 13px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid var(--border);
      background: var(--panel-alt);
    }
    .badge.green { color: var(--green); border-color: rgba(63,185,80,0.4); }
    .badge.cyan { color: var(--accent); border-color: rgba(88,166,255,0.4); }
    .badge.yellow { color: var(--yellow); border-color: rgba(210,153,34,0.4); }
    .badge.red { color: var(--red); border-color: rgba(248,81,73,0.4); }
    .badge.purple { color: var(--purple); border-color: rgba(188,140,255,0.4); }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 18px;
      align-items: start;
    }
    @media (max-width: 860px) {
      .grid { grid-template-columns: 1fr; }
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      margin-bottom: 16px;
      min-width: 0;
    }
    label {
      display: block;
      font-size: 13px;
      font-weight: 600;
      margin-bottom: 6px;
      color: var(--text);
    }
    textarea, input[type="text"], select {
      width: 100%;
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      font-size: 14px;
      font-family: inherit;
      outline: none;
    }
    textarea:focus, input[type="text"]:focus, select:focus {
      border-color: var(--accent);
    }
    textarea {
      min-height: 220px;
      resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    .row {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
      margin-top: 12px;
    }
    @media (max-width: 580px) {
      .row { grid-template-columns: 1fr; }
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 6px;
    }
    .chip {
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 6px;
      background: var(--panel-alt);
      border: 1px solid var(--border);
      color: var(--muted);
      cursor: pointer;
      user-select: none;
    }
    .chip:hover { color: var(--text); border-color: var(--accent); }
    .actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 14px;
    }
    .check-label {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      margin: 0;
    }
    button.primary {
      background: #238636;
      color: #fff;
      border: 1px solid rgba(240,246,252,0.1);
      border-radius: 8px;
      padding: 9px 18px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    button.primary:hover { background: #2ea043; }
    button.primary:disabled { opacity: 0.55; cursor: not-allowed; }
    .decision-box {
      display: none;
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 12px;
      font-size: 13px;
    }
    .decision-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    pre#output {
      margin: 0;
      background: #090d11;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      min-height: 780px;
      max-height: 1620px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12.5px;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
      color: #d0d7de;
    }
    .job-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: calc(100vh - 200px);
      overflow-y: auto;
    }
    .job-item {
      padding: 9px 10px;
      border-radius: 8px;
      background: var(--panel-alt);
      border: 1px solid var(--border);
      cursor: pointer;
      font-size: 12.5px;
    }
    .job-item:hover, .job-item.active {
      border-color: var(--accent);
    }
    .job-item .job-task {
      font-weight: 600;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-bottom: 4px;
    }
    .job-item .job-meta {
      display: flex;
      justify-content: space-between;
      color: var(--muted);
      font-size: 11px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1>⚡ JRC Web Konsolu <span class="badge cyan">127.0.0.1</span></h1>
        <div class="sub">Görevleri tarayıcıdan Jev Router &amp; Yönetici (Director) akışına gönderin</div>
      </div>
      <div id="statusBadge" class="badge">Hazır</div>
    </header>

    <div class="grid">
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
          <strong style="font-size:14px;">Geçmiş Görevler</strong>
          <button type="button" id="refreshJobsBtn" class="chip">Yenile</button>
        </div>
        <div id="jobList" class="job-list">
          <div class="sub">Yükleniyor...</div>
        </div>
      </div>

      <form id="runForm" class="card">
        <div>
          <label for="task">Görev / Prompt <span class="sub">(Ctrl+Enter ile gönder)</span></label>
          <textarea id="task" name="task" placeholder="Yapılacak işi yazın (örn. merhaba dünya dosyası oluştur)..." required></textarea>
        </div>

        <div class="row">
          <div>
            <label for="cwd">Çalışma Dizini (cwd)</label>
            <input type="text" id="cwd" name="cwd" value="__DEFAULT_CWD__" required />
            <div class="chips">
              <span class="chip" data-cwd="~/Desktop/jev/projeler">~/Desktop/jev/projeler</span>
              <span class="chip" data-cwd="~/Desktop/jev">~/Desktop/jev</span>
              <span class="chip" data-cwd="~/Desktop/jev/projeler/odoo">~/Desktop/jev/projeler/odoo</span>
              <span class="chip" data-cwd="~/Desktop/jev/projeler/Webler">~/Desktop/jev/projeler/Webler</span>
              <span class="chip" data-cwd="/tmp">/tmp</span>
            </div>
          </div>
          <div>
            <label for="tool">Araç Seçimi</label>
            <select id="tool" name="tool">
              <option value="auto">Otomatik (Jev seçer)</option>
              <option value="claude">Claude Code (claude)</option>
              <option value="agy">Antigravity (agy)</option>
            </select>
          </div>
        </div>

        <div class="actions">
          <label class="check-label">
            <input type="checkbox" id="dryRun" name="dry_run" />
            Dry-run (yalnızca karar testi, komutu çalıştırma)
          </label>
          <button type="submit" id="submitBtn" class="primary">🚀 Gönder</button>
        </div>
      </form>

      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
          <strong style="font-size:14px;">Karar &amp; Canlı Çıktı</strong>
          <span id="jobTimer" class="sub"></span>
        </div>

        <div id="decisionBox" class="decision-box">
          <div id="decisionGrid" class="decision-grid"></div>
        </div>

        <pre id="output">Henüz görev başlatılmadı.</pre>
      </div>
    </div>
  </div>

  <script>
    const JRC_TOKEN = __JRC_TOKEN_JSON__;
    const SERVER_LAST_CWD = __LAST_CWD_JSON__;

    const taskEl = document.getElementById('task');
    const cwdEl = document.getElementById('cwd');
    const toolEl = document.getElementById('tool');
    const dryRunEl = document.getElementById('dryRun');
    const submitBtn = document.getElementById('submitBtn');
    const outputEl = document.getElementById('output');
    const decisionBox = document.getElementById('decisionBox');
    const decisionGrid = document.getElementById('decisionGrid');
    const statusBadge = document.getElementById('statusBadge');
    const jobTimer = document.getElementById('jobTimer');
    const jobListEl = document.getElementById('jobList');

    let activeJobId = null;
    let pollTimer = null;
    let startTimeMs = null;

    // Son kullanılan cwd'yi hatırla
    const savedCwd = localStorage.getItem('jrc_web_last_cwd');
    if (savedCwd) {
      cwdEl.value = savedCwd;
    } else if (SERVER_LAST_CWD) {
      cwdEl.value = SERVER_LAST_CWD;
    }

    document.querySelectorAll('.chip[data-cwd]').forEach(chip => {
      chip.addEventListener('click', () => {
        cwdEl.value = chip.getAttribute('data-cwd');
        localStorage.setItem('jrc_web_last_cwd', cwdEl.value);
      });
    });

    taskEl.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        document.getElementById('runForm').requestSubmit();
      }
    });

    function renderDecision(dec, dryRun) {
      if (!dec) {
        decisionBox.style.display = 'none';
        return;
      }
      decisionBox.style.display = 'block';
      const toolClass = dec.tool === 'claude' ? 'green' : 'cyan';
      const m = dec.metrics || {};
      decisionGrid.innerHTML = '';

      const items = [
        { text: `Araç: ${dec.tool}`, cls: toolClass },
        { text: `Model: ${dec.model}`, cls: 'purple' },
        { text: `Efor: ${dec.effort}`, cls: 'yellow' },
      ];
      if (dec.label) {
        items.push({ text: dec.label, cls: '' });
      }
      if (m.complexity_level) {
        items.push({ text: `Karmaşıklık: ${m.complexity_level} (${m.complexity_score})`, cls: '' });
      }
      if (dryRun) {
        items.push({ text: 'DRY-RUN', cls: 'yellow' });
      }

      for (const it of items) {
        const span = document.createElement('span');
        span.className = 'badge ' + (it.cls || '');
        span.textContent = it.text;
        decisionGrid.appendChild(span);
      }
    }

    function setStatus(status, exitCode, queuePos) {
      if (status === 'queued') {
        statusBadge.className = 'badge purple';
        statusBadge.textContent = queuePos ? `🕒 Sırada (#${queuePos})` : '🕒 Sırada';
      } else if (status === 'cancelled') {
        statusBadge.className = 'badge';
        statusBadge.textContent = '⊘ İptal edildi';
      } else if (status === 'running') {
        statusBadge.className = 'badge yellow';
        statusBadge.textContent = '⏳ Çalışıyor...';
      } else if (status === 'done') {
        statusBadge.className = 'badge green';
        statusBadge.textContent = '✓ Tamamlandı';
      } else if (status === 'error') {
        statusBadge.className = 'badge red';
        statusBadge.textContent = `✗ Hata (kod: ${exitCode ?? 1})`;
      } else {
        statusBadge.className = 'badge';
        statusBadge.textContent = 'Hazır';
      }
    }

    async function pollJob(jobId) {
      if (pollTimer) clearTimeout(pollTimer);
      try {
        const res = await fetch(`/api/job/${encodeURIComponent(jobId)}`, {
          headers: { 'X-JRC-Token': JRC_TOKEN }
        });
        const data = await res.json();
        if (!data.ok) {
          outputEl.textContent = 'Hata: ' + (data.error || 'İş okunamadı');
          submitBtn.disabled = false;
          return;
        }
        renderDecision(data.decision, data.dry_run);
        if (data.status === 'queued') {
          outputEl.textContent = (data.output || '') + `🕒 Sırada bekliyor (#${data.queue_pos || '?'}). Önceki görev bitince otomatik başlayacak.`;
        } else {
          outputEl.textContent = data.output || '(Çıktı bekleniyor...)';
        }
        outputEl.scrollTop = outputEl.scrollHeight;
        setStatus(data.status, data.exit_code, data.queue_pos);

        if (startTimeMs && data.status === 'running') {
          const sec = ((Date.now() - startTimeMs) / 1000).toFixed(1);
          jobTimer.textContent = `${sec} sn`;
        } else if (data.finished_at) {
          jobTimer.textContent = data.finished_at;
        }

        if (data.status === 'running' || data.status === 'queued') {
          if (data.status === 'running' && lastPolledStatus === 'queued') startTimeMs = Date.now();
          pollTimer = setTimeout(() => pollJob(jobId), data.status === 'queued' ? 1500 : 650);
          if (data.status !== lastPolledStatus) loadJobs();
        } else {
          loadJobs();
        }
        lastPolledStatus = data.status;
      } catch (err) {
        submitBtn.disabled = false;
      }
    }

    let lastPolledStatus = null;

    async function cancelJob(jobId) {
      try {
        await fetch('/api/cancel', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-JRC-Token': JRC_TOKEN },
          body: JSON.stringify({ id: jobId })
        });
      } catch (e) {}
      loadJobs();
      if (jobId === activeJobId) pollJob(jobId);
    }

    async function loadJobs() {
      try {
        const res = await fetch('/api/jobs', {
          headers: { 'X-JRC-Token': JRC_TOKEN }
        });
        const data = await res.json();
        const jobs = data.jobs || [];
        if (!jobs.length) {
          jobListEl.innerHTML = '<div class="sub">Henüz kayıtlı görev yok.</div>';
          return;
        }
        jobListEl.innerHTML = '';
        for (const j of jobs) {
          const div = document.createElement('div');
          div.className = 'job-item' + (j.id === activeJobId ? ' active' : '');
          const tDiv = document.createElement('div');
          tDiv.className = 'job-task';
          tDiv.textContent = (j.dry_run ? '[DRY] ' : '') + (j.task || '');
          const mDiv = document.createElement('div');
          mDiv.className = 'job-meta';
          const decTag = j.decision ? `${j.decision.tool}:${j.decision.model}` : (j.tool || 'auto');
          const stIcon = j.status === 'done' ? '✓' : (j.status === 'running' ? '⏳'
            : (j.status === 'queued' ? `🕒 #${j.queue_pos || '?'}` : (j.status === 'cancelled' ? '⊘' : '✗')));
          mDiv.textContent = `${stIcon} ${decTag} · ${(j.created_at || '').slice(5, 16)}`;
          div.appendChild(tDiv);
          div.appendChild(mDiv);
          if (j.status === 'queued') {
            const cBtn = document.createElement('button');
            cBtn.type = 'button';
            cBtn.className = 'chip';
            cBtn.textContent = '⊘ Sıradan çıkar';
            cBtn.style.marginTop = '4px';
            cBtn.addEventListener('click', (ev) => { ev.stopPropagation(); cancelJob(j.id); });
            div.appendChild(cBtn);
          }
          div.addEventListener('click', () => {
            activeJobId = j.id;
            startTimeMs = null;
            pollJob(j.id);
            loadJobs();
          });
          jobListEl.appendChild(div);
        }
      } catch (e) {
        jobListEl.innerHTML = '<div class="sub">Geçmiş yüklenemedi.</div>';
      }
    }

    document.getElementById('refreshJobsBtn').addEventListener('click', loadJobs);

    document.getElementById('runForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const task = taskEl.value.trim();
      const cwd = cwdEl.value.trim();
      if (!task) return;

      localStorage.setItem('jrc_web_last_cwd', cwd);
      submitBtn.disabled = true;
      setStatus('running');
      decisionBox.style.display = 'none';
      outputEl.textContent = 'Görev başlatılıyor...';
      startTimeMs = Date.now();
      jobTimer.textContent = '0.0 sn';

      try {
        const res = await fetch('/api/run', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-JRC-Token': JRC_TOKEN
          },
          body: JSON.stringify({
            task: task,
            cwd: cwd,
            tool: toolEl.value,
            dry_run: dryRunEl.checked
          })
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
          outputEl.textContent = '✗ Hata: ' + (data.error || res.statusText);
          setStatus('error', 1);
          submitBtn.disabled = false;
          return;
        }
        activeJobId = data.id;
        submitBtn.disabled = false;
        if (data.status === 'queued') {
          startTimeMs = null;
          jobTimer.textContent = '';
          taskEl.value = '';
        }
        loadJobs();
        pollJob(data.id);
      } catch (err) {
        outputEl.textContent = '✗ İstek hatası: ' + err;
        setStatus('error', 1);
        submitBtn.disabled = false;
      }
    });

    loadJobs();
    setInterval(loadJobs, 5000);
  </script>
</body>
</html>
"""


class JRCWebHandler(BaseHTTPRequestHandler):
    server_version = "JRCWeb/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Konsolu kirletmemek için yalnız hataları yazdır
        pass

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _check_auth(self, body_token: str | None = None) -> bool:
        expected = ensure_token()
        # DNS rebinding koruması: Host başlığı yalnızca 127.0.0.1 veya localhost olabilir
        host_hdr = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        if host_hdr and host_hdr not in ("127.0.0.1", "localhost"):
            return False

        # Tarayıcıdan gelen cross-origin (CSRF) isteklerini engelle
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and not (
            origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")
            or origin in ("http://127.0.0.1", "http://localhost")
        ):
            return False

        hdr_tok = (self.headers.get("X-JRC-Token") or "").strip()
        cookie_tok = ""
        if "Cookie" in self.headers:
            c = SimpleCookie()
            try:
                c.load(self.headers["Cookie"])
                if "jrc_token" in c:
                    cookie_tok = c["jrc_token"].value
            except Exception:
                pass
        parsed = urllib.parse.urlparse(self.path)
        qs_tok = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]

        provided = hdr_tok or (body_token or "").strip() or cookie_tok or qs_tok
        if provided:
            return secrets.compare_digest(provided, expected)

        # Servis kalıcı çalıştığı için her API isteğinde geçerli token zorunludur
        return False

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            tok = ensure_token()
            last_cwd = get_last_cwd()
            html = (
                HTML_TEMPLATE
                .replace("__DEFAULT_CWD__", last_cwd.replace('"', "&quot;"))
                .replace("__JRC_TOKEN_JSON__", json.dumps(tok))
                .replace("__LAST_CWD_JSON__", json.dumps(last_cwd))
            )
            raw = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", f"jrc_token={tok}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(raw)
            return

        if not self._check_auth():
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Yetkisiz istek (geçersiz web_token)."})
            return

        if path == "/api/jobs":
            jobs = list_jobs(25)
            pos = _queue_positions()
            for j in jobs:
                if j.get("id") in pos:
                    j["queue_pos"] = pos[j["id"]]
            self._send_json(HTTPStatus.OK, {"ok": True, "jobs": jobs, "queue_len": len(pos)})
            return

        if path.startswith("/api/job/"):
            job_id = path.split("/api/job/", 1)[1].strip("/")
            meta = _load_job_meta(job_id)
            if not meta:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"İş bulunamadı: {job_id}"})
                return

            qs = urllib.parse.parse_qs(parsed.query)
            try:
                offset = max(0, int(qs.get("offset", ["0"])[0]))
            except ValueError:
                offset = 0

            log_file = _job_log_path(job_id)
            full_output = ""
            chunk = ""
            next_offset = offset
            if log_file.exists():
                try:
                    raw_bytes = log_file.read_bytes()
                    full_output = strip_ansi(raw_bytes.decode("utf-8", errors="replace"))
                    chunk_bytes = raw_bytes[offset:]
                    chunk = strip_ansi(chunk_bytes.decode("utf-8", errors="replace"))
                    next_offset = len(raw_bytes)
                except OSError:
                    pass

            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    **meta,
                    "queue_pos": _queue_positions().get(job_id),
                    "output": full_output,
                    "chunk": chunk,
                    "next_offset": next_offset,
                },
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Sayfa bulunamadı."})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/api/run", "/api/cancel"):
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Uç nokta bulunamadı."})
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        raw_body = self.rfile.read(max(0, min(length, 512_000)))
        try:
            data = json.loads(raw_body.decode("utf-8") or "{}")
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Geçersiz JSON gövdesi."})
            return

        if not self._check_auth(body_token=data.get("token")):
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Yetkisiz istek (geçersiz web_token)."})
            return

        if parsed.path == "/api/cancel":
            ok, msg = cancel_queued(str(data.get("id") or "").strip())
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST, {"ok": ok, "message": msg, "error": None if ok else msg})
            _kick_queue()
            return

        task = str(data.get("task") or "").strip()
        if not task:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Görev metni (task) boş olamaz."})
            return

        raw_cwd = str(data.get("cwd") or "").strip() or get_last_cwd()
        cwd_path = Path(os.path.expanduser(raw_cwd)).resolve()
        if not cwd_path.exists() or not cwd_path.is_dir():
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": f"Çalışma dizini bulunamadı: {cwd_path}"},
            )
            return

        raw_tool = str(data.get("tool") or "").strip().lower()
        tool = raw_tool if raw_tool in ("claude", "agy") else "auto"
        dry_run = bool(data.get("dry_run", False))

        set_last_cwd(raw_cwd)

        job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        WEB_JOBS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _job_log_path(job_id)
        log_path.write_text("", encoding="utf-8")

        meta: dict[str, Any] = {
            "id": job_id,
            "task": task,
            "cwd": str(cwd_path),
            "tool": tool,
            "dry_run": dry_run,
            "status": "queued",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "queued_at": f"{time.time():.6f}",
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "pid": None,
            "decision": None,
            "command": None,
        }
        with _QUEUE_COND:
            _save_job_meta(job_id, meta)
            if dry_run:
                # Dry-run ajan çalıştırmaz; sırayı beklemeden hemen yürür
                _launch_job(meta)
            else:
                _dispatch_once()
            meta = _load_job_meta(job_id) or meta
            pos = _queue_positions().get(job_id)
            _QUEUE_COND.notify_all()

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "id": job_id,
                "job_id": job_id,
                "status": meta.get("status"),
                "queue_pos": pos,
                "cwd": str(cwd_path),
            },
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jrc web",
        description="JRC Yerel Web Arayüzü (yalnızca 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Dinlenecek yerel port (varsayılan: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--worker",
        dest="worker_job_id",
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    if args.worker_job_id:
        return run_worker(args.worker_job_id)

    tok = ensure_token()
    WEB_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_dispatcher_loop, name="jrc-queue", daemon=True).start()

    try:
        server = ThreadingHTTPServer((HOST, args.port), JRCWebHandler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                f"\033[31m✗ Hata: {HOST}:{args.port} portu zaten kullanımda.\033[0m\n"
                f"  Farklı bir portla başlatmak için: jrc web --port {args.port + 1}",
                file=sys.stderr,
            )
        else:
            print(f"\033[31m✗ Sunucu başlatılamadı ({HOST}:{args.port}): {exc}\033[0m", file=sys.stderr)
        return 1

    url = f"http://{HOST}:{args.port}/"
    print(
        f"\033[1m⚡ JRC Web Konsolu aktif\033[0m\n"
        f"  ├─ Adres   : \033[36m{url}\033[0m (yalnızca yerel 127.0.0.1)\n"
        f"  ├─ Token   : \033[2m{TOKEN_FILE} (0600)\033[0m\n"
        f"  └─ Geçmiş  : \033[2m{WEB_JOBS_DIR}\033[0m\n"
        f"\033[2mDurdurmak için Ctrl+C basın.\033[0m",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\033[2mJRC Web kapatıldı.\033[0m")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
