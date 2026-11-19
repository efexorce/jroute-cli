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
import jrc_tokens  # noqa: E402
import jrc_usage  # noqa: E402
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
DEFAULT_CWD = os.environ.get("JRC_WEB_CWD") or str(Path(__file__).resolve().parent)

CACHE_DIR = Path.home() / ".cache" / "jroute"
TOKEN_FILE = CACHE_DIR / "web_token"
WEB_JOBS_DIR = CACHE_DIR / "web_jobs"
STATE_FILE = CACHE_DIR / "web_state.json"
EMIR_SEQ_FILE = CACHE_DIR / "emir_seq.json"
ODOO_TOKEN_SYNC_FILE = Path("/home/efe/Desktop/jev/projeler/odoo/addons/.jrc_web_token")
ODOO_BRIDGE_DIR = Path("/home/efe/Desktop/jev/projeler/odoo/addons/.jrc_ai_bridge")
ODOO_AUDIT_LOG = CACHE_DIR / "odoo_ai_audit.jsonl"

IMAGE_ALLOWED_ROOTS: tuple[str, ...] = (
    "/home/efe/Desktop/jev/artifacts",
    "/home/efe/Desktop/jev/projeler",
    "/home/efe/Desktop/projeler",
    str(Path(DEFAULT_CWD).resolve() / "artifacts"),
)
IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")
DIRECTOR_ORDER_RE = re.compile(
    r"🎬 Yönetici \([^)]+\):[^\n]*iş emri hazırlanıyor[^\n]*\n\x1b\[2m[\s\S]*?\x1b\[0m\r?\n+"
)
ROUTER_CMD_RE = re.compile(r"^\s*└─ Komut\s*:[^\n]*\n+", flags=re.MULTILINE)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def extract_result_section(raw_text: str, dry_run: bool = False) -> str:
    """Çıktının son anlamlı bölümünü (ajanın nihai yanıtını) ayıklar."""
    if not raw_text or not raw_text.strip():
        return ""
    if dry_run:
        return strip_ansi(raw_text).strip()
    m_dir = DIRECTOR_ORDER_RE.search(raw_text)
    if m_dir:
        return strip_ansi(raw_text[m_dir.end():]).strip()
    clean = strip_ansi(raw_text)
    m_cmd = ROUTER_CMD_RE.search(clean)
    if m_cmd:
        tail = clean[m_cmd.end():].strip()
        if tail:
            return tail
    lines = clean.strip().splitlines()
    return "\n".join(lines[-40:])


def _sync_odoo_token(tok: str) -> None:
    try:
        if ODOO_TOKEN_SYNC_FILE.parent.is_dir():
            ODOO_TOKEN_SYNC_FILE.write_text(tok + "\n", encoding="utf-8")
            os.chmod(ODOO_TOKEN_SYNC_FILE, 0o644)
    except OSError:
        pass


def ensure_token() -> str:
    """~/.cache/jroute/web_token dosyasını 0600 izinle üretir veya okur."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        try:
            tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if tok:
                os.chmod(TOKEN_FILE, 0o600)
                _sync_odoo_token(tok)
                return tok
        except OSError:
            pass
    tok = secrets.token_urlsafe(24)
    fd = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    _sync_odoo_token(tok)
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
    tmp = WEB_JOBS_DIR / f".{p.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def _load_emir_seq() -> tuple[int, dict[str, int]]:
    """emir_seq.json dosyasından (seq, {job_id: no}) bilgisini okur."""
    if not EMIR_SEQ_FILE.exists():
        return 0, {}
    try:
        raw = json.loads(EMIR_SEQ_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, {}
    if isinstance(raw, int):
        return max(0, raw), {}
    if isinstance(raw, dict):
        seq_val = raw.get("seq", raw.get("no", 0))
        seq = int(seq_val) if isinstance(seq_val, (int, float)) else 0
        assigned_raw = raw.get("assigned")
        assigned: dict[str, int] = {}
        if isinstance(assigned_raw, dict):
            for k, v in assigned_raw.items():
                if isinstance(v, int) and v > 0:
                    assigned[str(k)] = v
        return max(0, seq), assigned
    return 0, {}


def _save_emir_seq(seq: int, assigned: dict[str, int]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "seq": int(seq),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "assigned": assigned,
    }
    tmp = CACHE_DIR / f".emir_seq.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(EMIR_SEQ_FILE)


def _queued_float(val: Any) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def backfill_emir_numbers() -> int:
    """`no` alanı olmayan eski web_jobs meta kayıtlarını created_at sırasına göre idempotent numaralandırır."""
    WEB_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    seq, orig_assigned = _load_emir_seq()
    assigned = dict(orig_assigned)
    max_no = max([seq, *assigned.values()]) if assigned else seq

    unnumbered: list[tuple[Path, str, dict[str, Any], tuple[float, float] | None]] = []
    dirty_seq = not EMIR_SEQ_FILE.exists()

    for p in sorted(WEB_JOBS_DIR.glob("*.json")):
        try:
            st = p.stat()
            utimes: tuple[float, float] | None = (st.st_atime, st.st_mtime)
            meta = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue

        jid = str(meta.get("id") or p.stem)
        existing_no = meta.get("no")
        if isinstance(existing_no, int) and existing_no > 0:
            if assigned.get(jid) != existing_no:
                assigned[jid] = existing_no
                dirty_seq = True
            if existing_no > max_no:
                max_no = existing_no
            continue

        # Eski bir worker bellekteki eski meta ile dosyayı ezdiyse atanan numarayı geri yükle
        if jid in assigned and assigned[jid] > 0:
            restored_no = assigned[jid]
            meta["no"] = restored_no
            _save_job_meta(jid, meta)
            if utimes:
                try:
                    os.utime(_job_meta_path(jid), utimes)
                except OSError:
                    pass
            if restored_no > max_no:
                max_no = restored_no
            continue

        unnumbered.append((p, jid, meta, utimes))

    if unnumbered:
        unnumbered.sort(
            key=lambda item: (
                str(item[2].get("created_at") or ""),
                _queued_float(item[2].get("queued_at")),
                item[1],
            )
        )
        cur = max_no
        for _p, jid, meta, utimes in unnumbered:
            cur += 1
            meta["no"] = cur
            _save_job_meta(jid, meta)
            if utimes:
                try:
                    os.utime(_job_meta_path(jid), utimes)
                except OSError:
                    pass
            assigned[jid] = cur
        max_no = cur
        dirty_seq = True

    if dirty_seq or max_no != seq:
        _save_emir_seq(max_no, assigned)
    return max_no


def _next_emir_no(job_id: str) -> int:
    """`_QUEUE_COND` kilidi altında çağrılmalıdır: sayacı 1 artırır ve yeni numarayı döndürür."""
    if not EMIR_SEQ_FILE.exists():
        backfill_emir_numbers()
    seq, assigned = _load_emir_seq()
    if assigned:
        seq = max(seq, max(assigned.values()))
    next_no = seq + 1
    assigned[job_id] = next_no
    _save_emir_seq(next_no, assigned)
    return next_no


def _load_job_meta(job_id: str) -> dict[str, Any] | None:
    p = _job_meta_path(job_id)
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    changed = False
    if not (isinstance(meta.get("no"), int) and meta["no"] > 0):
        _, assigned = _load_emir_seq()
        safe_id = p.stem
        assigned_no = assigned.get(str(meta.get("id") or safe_id)) or assigned.get(safe_id)
        if isinstance(assigned_no, int) and assigned_no > 0:
            meta["no"] = assigned_no
            changed = True
    if meta.get("status") == "running" and meta.get("pid"):
        try:
            os.kill(int(meta["pid"]), 0)
        except ProcessLookupError:
            # Alt süreç beklenmedik şekilde kapandıysa durumu güncelle
            meta["status"] = "error"
            meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            changed = True
        except PermissionError:
            pass
    if changed:
        _save_job_meta(job_id, meta)
    return meta


def list_jobs(limit: int = 200) -> list[dict[str, Any]]:
    WEB_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for f in WEB_JOBS_DIR.glob("*.json"):
        meta = _load_job_meta(f.stem)
        if meta:
            items.append(meta)
    items.sort(
        key=lambda m: (
            int(m.get("no") or 0),
            str(m.get("created_at") or ""),
            str(m.get("id") or ""),
        ),
        reverse=True,
    )
    return items[:limit]


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


def _record_odoo_audit(entry: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with ODOO_AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def enqueue_job(
    task: str,
    tool: str = "auto",
    dry_run: bool = False,
    user_id: int | None = None,
    user_name: str | None = None,
    user_login: str | None = None,
    source: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    task = str(task or "").strip()
    if not task:
        return False, {"ok": False, "error": "Görev metni (task) boş olamaz."}

    cwd_path = Path(os.path.expanduser(DEFAULT_CWD)).resolve()
    if not cwd_path.exists() or not cwd_path.is_dir():
        return False, {"ok": False, "error": f"Çalışma dizini bulunamadı: {cwd_path}"}

    raw_tool = str(tool or "").strip().lower()
    norm_tool = raw_tool if raw_tool in ("claude", "agy") else "auto"
    is_dry = bool(dry_run)

    with _QUEUE_COND:
        _reap_procs()
        # Kullanıcı başı eşzamanlı/açık iş sınırı (Hacker Bey id=2 hariç, en çok 2 açık iş)
        if user_id is not None and int(user_id) != 2 and not is_dry:
            active_for_user = [
                m for m in _all_job_metas()
                if m.get("user_id") == int(user_id) and m.get("status") in ("queued", "running")
            ]
            if len(active_for_user) >= 2:
                return False, {
                    "ok": False,
                    "error": "Aynı anda en fazla 2 açık/bekleyen göreviniz olabilir. Lütfen mevcut görevin tamamlanmasını bekleyin veya sıradakini iptal edin.",
                }

        job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        WEB_JOBS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _job_log_path(job_id)
        log_path.write_text("", encoding="utf-8")

        meta: dict[str, Any] = {
            "id": job_id,
            "task": task,
            "cwd": str(cwd_path),
            "tool": norm_tool,
            "dry_run": is_dry,
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
        if user_id is not None:
            meta["user_id"] = int(user_id)
        if user_name:
            meta["user_name"] = str(user_name)
        if user_login:
            meta["user_login"] = str(user_login)
        if source:
            meta["source"] = str(source)

        meta["no"] = _next_emir_no(job_id)
        _save_job_meta(job_id, meta)
        if is_dry:
            _launch_job(meta)
        else:
            _dispatch_once()
        meta = _load_job_meta(job_id) or meta
        pos = _queue_positions().get(job_id)
        _QUEUE_COND.notify_all()

    if user_id is not None or source:
        _record_odoo_audit({
            "time": meta.get("created_at"),
            "job_id": job_id,
            "no": meta.get("no"),
            "user_id": meta.get("user_id"),
            "user_name": meta.get("user_name"),
            "user_login": meta.get("user_login"),
            "source": meta.get("source") or "jrc_web",
            "tool": norm_tool,
            "dry_run": is_dry,
            "task": task,
        })

    return True, {
        "ok": True,
        "id": job_id,
        "job_id": job_id,
        "no": meta.get("no"),
        "status": meta.get("status"),
        "queue_pos": pos,
        "cwd": str(cwd_path),
        "user_id": meta.get("user_id"),
        "user_name": meta.get("user_name"),
    }


def _job_detail_payload(job_id: str, offset: int = 0) -> tuple[int, dict[str, Any]]:
    meta = _load_job_meta(job_id)
    if not meta:
        return int(HTTPStatus.NOT_FOUND), {"ok": False, "error": f"İş bulunamadı: {job_id}"}

    log_file = _job_log_path(job_id)
    full_output = ""
    result_output = ""
    chunk = ""
    next_offset = max(0, offset)
    if log_file.exists():
        try:
            raw_bytes = log_file.read_bytes()
            raw_str = raw_bytes.decode("utf-8", errors="replace")
            full_output = strip_ansi(raw_str)
            result_output = extract_result_section(raw_str, bool(meta.get("dry_run")))
            chunk_bytes = raw_bytes[next_offset:]
            chunk = strip_ansi(chunk_bytes.decode("utf-8", errors="replace"))
            next_offset = len(raw_bytes)
        except OSError:
            pass

    return int(HTTPStatus.OK), {
        "ok": True,
        "ui_ver": 5,
        **meta,
        "queue_pos": _queue_positions().get(job_id),
        "output": full_output,
        "result_output": result_output,
        "chunk": chunk,
        "next_offset": next_offset,
    }


def _list_jobs_for_user(uid: int | None, limit: int = 200) -> dict[str, Any]:
    jobs = list_jobs(limit)
    pos = _queue_positions()
    filtered: list[dict[str, Any]] = []
    for j in jobs:
        if uid is not None and int(uid) != 2:
            if j.get("user_id") != int(uid):
                continue
        j_copy = dict(j)
        if j_copy.get("id") in pos:
            j_copy["queue_pos"] = pos[j_copy["id"]]
        # Kısa özet (kart altında açılır özet için)
        log_file = _job_log_path(str(j_copy.get("id") or ""))
        if log_file.exists():
            try:
                raw_str = log_file.read_bytes().decode("utf-8", errors="replace")
                res_sec = extract_result_section(raw_str, bool(j_copy.get("dry_run")))
                lines = [ln.strip() for ln in res_sec.splitlines() if ln.strip()]
                j_copy["summary"] = "\n".join(lines[:4])[:360] if lines else ""
            except OSError:
                j_copy["summary"] = ""
        else:
            j_copy["summary"] = ""
        filtered.append(j_copy)
    return {"ok": True, "ui_ver": 5, "jobs": filtered, "queue_len": len(pos)}


def _handle_bridge_request(req: dict[str, Any]) -> dict[str, Any]:
    expected = ensure_token()
    provided = str(req.get("token") or "").strip()
    if not provided or not secrets.compare_digest(provided, expected):
        return {"ok": False, "status_code": 403, "error": "Yetkisiz istek (geçersiz web_token)."}

    action = str(req.get("action") or "").strip().lower()
    uid_raw = req.get("user_id")
    uid = int(uid_raw) if isinstance(uid_raw, int) or (isinstance(uid_raw, str) and uid_raw.isdigit()) else None
    uname = str(req.get("user_name") or "").strip() or None
    ulogin = str(req.get("user_login") or "").strip() or None

    if action == "run":
        ok, payload = enqueue_job(
            task=str(req.get("task") or ""),
            tool=str(req.get("tool") or "auto"),
            dry_run=bool(req.get("dry_run", False)),
            user_id=uid,
            user_name=uname,
            user_login=ulogin,
            source=str(req.get("source") or "odoo_yonetim"),
        )
        return {**payload, "status_code": 200 if ok else 400}

    if action == "jobs":
        return {**_list_jobs_for_user(uid, limit=200), "status_code": 200}

    if action == "job":
        job_id = str(req.get("id") or req.get("job_id") or "").strip()
        meta = _load_job_meta(job_id)
        if not meta:
            return {"ok": False, "status_code": 404, "error": f"İş bulunamadı: {job_id}"}
        if uid is not None and uid != 2 and meta.get("user_id") != uid:
            return {"ok": False, "status_code": 403, "error": "Bu görevi görüntüleme yetkiniz yok."}
        try:
            offset = max(0, int(req.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        code, payload = _job_detail_payload(job_id, offset=offset)
        return {**payload, "status_code": code}

    if action == "cancel":
        job_id = str(req.get("id") or req.get("job_id") or "").strip()
        meta = _load_job_meta(job_id)
        if not meta:
            return {"ok": False, "status_code": 404, "error": f"İş bulunamadı: {job_id}"}
        if uid is not None and uid != 2 and meta.get("user_id") != uid:
            return {"ok": False, "status_code": 403, "error": "Başka kullanıcının görevini iptal edemezsiniz."}
        ok, msg = cancel_queued(job_id)
        _kick_queue()
        return {"ok": ok, "status_code": 200 if ok else 400, "message": msg, "error": None if ok else msg}

    return {"ok": False, "status_code": 400, "error": f"Bilinmeyen köprü komutu: {action}"}


def _bridge_loop() -> None:
    """Odoo Docker konteynerinden (/mnt/extra-addons/.jrc_ai_bridge) gelen istekleri işler."""
    try:
        if ODOO_BRIDGE_DIR.parent.is_dir():
            ODOO_BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(ODOO_BRIDGE_DIR, 0o777)
    except OSError:
        pass
    ensure_token()
    last_clean = 0.0
    while True:
        try:
            if ODOO_BRIDGE_DIR.is_dir():
                for req_path in sorted(ODOO_BRIDGE_DIR.glob("*.req.json")):
                    stem = req_path.name[:-len(".req.json")]
                    res_tmp = ODOO_BRIDGE_DIR / f".{stem}.res.tmp"
                    res_path = ODOO_BRIDGE_DIR / f"{stem}.res.json"
                    try:
                        req_data = json.loads(req_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        req_path.unlink(missing_ok=True)
                        continue
                    req_path.unlink(missing_ok=True)
                    resp = _handle_bridge_request(req_data if isinstance(req_data, dict) else {})
                    try:
                        res_tmp.write_text(json.dumps(resp, ensure_ascii=False), encoding="utf-8")
                        os.chmod(res_tmp, 0o666)
                        res_tmp.replace(res_path)
                    except OSError:
                        pass
                now = time.time()
                if now - last_clean > 30:
                    last_clean = now
                    for old_f in ODOO_BRIDGE_DIR.glob("*.res.json"):
                        try:
                            if now - old_f.stat().st_mtime > 60:
                                old_f.unlink(missing_ok=True)
                        except OSError:
                            pass
        except Exception as exc:
            print(f"Odoo AI köprü hatası: {exc}", file=sys.stderr, flush=True)
        time.sleep(0.08)


def run_worker(job_id: str) -> int:
    """Arka plan görev işçisi: jrc konsolunun run_agent akışıyla birebir aynı yoldan yürütür."""
    meta = _load_job_meta(job_id)
    if not meta:
        print(f"✗ İş kaydı bulunamadı: {job_id}", file=sys.stderr, flush=True)
        return 1

    task = str(meta.get("task") or "").strip()
    emir_no = meta.get("no") if isinstance(meta.get("no"), int) else None
    target_cwd = Path(os.path.expanduser(str(meta.get("cwd") or DEFAULT_CWD))).resolve()
    raw_tool = str(meta.get("tool") or "").strip().lower()
    forced_tool = raw_tool if raw_tool in ("claude", "agy") else None
    dry_run = bool(meta.get("dry_run", False))
    emir_nums = jrc_context.extract_emir_numbers(task)
    emir_ctx = jrc_context.build_emir_context_block(task) if emir_nums else ""

    # Hız: yönetici iş emrini jev kararını beklemeden, analizle paralel yazmaya başlar.
    prefetch = None if dry_run else jrc_director.start_prefetch(task, target_cwd)
    try:
        print("⚡ Jev görev analizi yapılıyor..." + (" (yönetici iş emri paralel başladı)" if prefetch else ""), flush=True)
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
        prefetch = jrc_director.resolve_prefetch(prefetch, decision)
        director_str = (
            f"açık ({jrc_director.director_name()})"
            if director_active
            else "atlandı / doğrudan yürütme"
        )
        cmd_preview = " ".join(
            repr(a[:60] + ("…" if len(a) > 60 else "")) if " " in a else a for a in cmd
        )
        emir_line = f"  ├─ Emir No       : emir{emir_no}\n" if emir_no else ""
        user_line = (
            f"  ├─ Kullanıcı     : {meta.get('user_name')} (uid={meta.get('user_id')})\n"
            if meta.get("user_name")
            else ""
        )
        ref_line = (
            f"  ├─ Emir Bağlamı  : {', '.join(f'emir{n}' for n in emir_nums)} ({len(emir_ctx)} karakter)\n"
            if emir_nums
            else ""
        )
        print(
            f"⚡ Jev Router [{decision.get('decision_model', '')} · {decision.get('latency_ms', 0)} ms]\n"
            f"{emir_line}"
            f"{user_line}"
            f"  ├─ Araç & Model  : {decision['tool']} → {decision['model']} ({decision.get('label', '')})\n"
            f"  ├─ Efor & Risk   : effort={decision['effort']} | "
            f"karmaşıklık={m.get('complexity_level', '?')} ({m.get('complexity_score', 0)}) | "
            f"kritiklik={m.get('critical_probability', 0)}\n"
            f"  ├─ Yönetici      : {director_str}\n"
            f"{ref_line}"
            f"  ├─ Çalışma Dizini: {target_cwd}\n"
            f"  └─ Komut         : {cmd_preview}\n",
            flush=True,
        )

        if dry_run:
            if emir_ctx:
                print(f"{emir_ctx}\n", flush=True)
            print("✓ Dry-run tamamlandı (komut çalıştırılmadı).", flush=True)
            meta["status"] = "done"
            meta["exit_code"] = 0
            meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_job_meta(job_id, meta)
            return 0

        # Görev öncesi .jrc_context.md ortak hafıza ve gate.jsonl defter kaydı
        try:
            jrc_context.record_task_and_sync(target_cwd, task, decision, no=emir_no)
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
                prefetch=prefetch,
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
        if prefetch:
            prefetch.cancel()
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
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%230d1117' stroke='%2330363d' stroke-width='2'/%3E%3Ctext x='50%25' y='52%25' dominant-baseline='central' text-anchor='middle' font-family='system-ui,-apple-system,sans-serif' font-weight='800' font-size='42' fill='%2358a6ff'%3EJ%3C/text%3E%3C/svg%3E" />
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
    textarea, input[type="text"], input[type="search"], select {
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
    textarea:focus, input[type="text"]:focus, input[type="search"]:focus, select:focus {
      border-color: var(--accent);
    }
    #jobSearch {
      padding: 7px 10px;
      font-size: 12.5px;
      margin-bottom: 10px;
      background: var(--panel-alt);
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
    .emir-tag {
      display: inline-block;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 11px;
      font-weight: 700;
      padding: 1px 6px;
      margin-right: 6px;
      border-radius: 5px;
      background: rgba(88, 166, 255, 0.16);
      color: var(--accent);
      border: 1px solid rgba(88, 166, 255, 0.35);
      vertical-align: baseline;
    }
    .col-mid {
      min-width: 0;
    }
    .res-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 10px;
    }
    .res-task {
      font-size: 13px;
      font-weight: 600;
      color: var(--text);
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 10px;
      margin-bottom: 8px;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 96px;
      overflow-y: auto;
    }
    .res-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      margin-bottom: 10px;
    }
    .res-body {
      background: #090d11;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      max-height: 520px;
      overflow-y: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12.5px;
      line-height: 1.5;
    }
    .res-line {
      white-space: pre-wrap;
      word-break: break-word;
      padding: 1px 6px;
      border-radius: 4px;
      color: var(--text);
    }
    .res-line.res-empty {
      min-height: 8px;
      padding: 0;
    }
    .res-line.res-green {
      color: var(--green);
      background: rgba(63, 185, 80, 0.08);
      border-left: 2px solid var(--green);
    }
    .res-line.res-red {
      color: var(--red);
      background: rgba(248, 81, 73, 0.1);
      border-left: 2px solid var(--red);
    }
    .res-line.res-yellow {
      color: var(--yellow);
      background: rgba(210, 153, 34, 0.08);
      border-left: 2px solid var(--yellow);
    }
    .res-line.res-muted {
      color: var(--muted);
    }
      .res-line.res-heading {
      color: var(--accent);
      font-weight: 600;
      margin-top: 4px;
    }
    .res-gallery {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 10px;
    }
    .res-thumb-card {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 6px;
      width: 154px;
      padding: 7px;
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 8px;
      cursor: zoom-in;
      color: var(--text);
      font-family: inherit;
      transition: border-color 0.15s, transform 0.15s;
    }
    .res-thumb-card:hover {
      border-color: var(--accent);
      transform: translateY(-1px);
    }
    .res-thumb-img {
      width: 100%;
      height: 168px;
      object-fit: cover;
      border-radius: 5px;
      background: #090d11;
      display: block;
    }
    .res-thumb-cap {
      width: 100%;
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      text-align: center;
    }
    .res-files {
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-top: 10px;
    }
    .res-file-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 10px;
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 8px;
      transition: border-color 0.15s;
    }
    .res-file-row:hover {
      border-color: var(--accent);
    }
    .res-file-name {
      font-size: 12.5px;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
      flex: 1;
    }
    .res-file-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }
    .res-file-btn {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 600;
      color: var(--text);
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      text-decoration: none;
      transition: border-color 0.15s, color 0.15s, background 0.15s;
    }
    .res-file-btn:hover {
      border-color: var(--accent);
      color: var(--accent);
    }
    .res-file-btn.primary {
      color: var(--accent);
      border-color: rgba(88, 166, 255, 0.4);
      background: rgba(88, 166, 255, 0.08);
    }
    .res-file-btn.primary:hover {
      background: rgba(88, 166, 255, 0.16);
      border-color: var(--accent);
    }
    .img-lightbox {
      position: fixed;
      inset: 0;
      z-index: 9999;
      background: rgba(5, 8, 13, 0.9);
      backdrop-filter: blur(4px);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 10px;
      padding: 24px;
      cursor: zoom-out;
    }
    .img-lightbox-close {
      position: fixed;
      top: 16px;
      right: 20px;
      background: var(--panel);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 12px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      font-family: inherit;
    }
    .img-lightbox-close:hover {
      border-color: var(--accent);
    }
    .img-lightbox-img {
      max-width: min(92vw, 1100px);
      max-height: calc(100vh - 96px);
      object-fit: contain;
      border-radius: 10px;
      border: 1px solid var(--border);
      box-shadow: 0 18px 50px rgba(0, 0, 0, 0.7);
      cursor: default;
      background: #090d11;
    }
    .img-lightbox-cap {
      max-width: 90vw;
      font-size: 12px;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      cursor: default;
    }
    /* Sekme navigasyonu ve Token Harcama paneli */
    .nav-tabs {
      display: inline-flex;
      gap: 6px;
      background: var(--panel);
      padding: 4px;
      border-radius: 10px;
      border: 1px solid var(--border);
    }
    .nav-tab {
      background: transparent;
      color: var(--muted);
      border: 1px solid transparent;
      border-radius: 7px;
      padding: 6px 14px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-family: inherit;
    }
    .nav-tab:hover {
      color: var(--text);
    }
    .nav-tab.active {
      background: var(--panel-alt);
      color: var(--text);
      border-color: var(--accent);
    }
    .tk-donem, .tk-olcu {
      display: inline-flex;
      gap: 5px;
      flex-wrap: wrap;
    }
    .tk-donem button, .tk-olcu button {
      border: 1px solid var(--border);
      background: var(--panel-alt);
      color: var(--muted);
      font-weight: 600;
      font-size: 12px;
      padding: 5px 11px;
      border-radius: 7px;
      cursor: pointer;
      font-family: inherit;
    }
    .tk-donem button:hover, .tk-olcu button:hover {
      color: var(--text);
      border-color: var(--accent);
    }
    .tk-donem button.aktif, .tk-olcu button.aktif {
      background: rgba(88, 166, 255, 0.16);
      border-color: var(--accent);
      color: var(--accent);
    }
    .tk-kpi {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(165px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .tk-kpi .kutu {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 11px 14px;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .tk-kpi .kutu small {
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
    }
    .tk-kpi .kutu b {
      font-size: 21px;
      color: var(--text);
      font-variant-numeric: tabular-nums;
    }
    .tk-motorlar {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .tk-motor {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px;
    }
    .tk-motor h4 {
      margin: 0 0 4px;
      font-size: 13px;
      color: var(--muted);
      display: flex;
      justify-content: space-between;
      gap: 8px;
    }
    .tk-motor b {
      font-size: 22px;
      color: var(--text);
      display: block;
      font-variant-numeric: tabular-nums;
    }
    .tk-motor small {
      color: var(--muted);
      font-size: 12px;
    }
    .tk-pay {
      height: 6px;
      background: var(--bg);
      border-radius: 3px;
      overflow: hidden;
      margin: 6px 0 4px;
    }
    .tk-pay i {
      display: block;
      height: 100%;
      background: var(--accent);
      border-radius: 3px;
    }
    .tk-card-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }
    .tk-card-head h3 {
      margin: 0;
      font-size: 14px;
      color: var(--text);
    }
    .tk-kaydir {
      overflow-x: auto;
    }
    .tk-tablo {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    .tk-tablo th {
      font-size: 12px;
      white-space: nowrap;
      text-align: left;
      color: var(--muted);
      padding: 7px 8px;
      border-bottom: 1px solid var(--border);
    }
    .tk-tablo td {
      padding: 7px 8px;
      border-bottom: 1px solid rgba(48, 54, 61, 0.55);
      color: var(--text);
    }
    .tk-tablo tr:last-child td {
      border-bottom: 0;
    }
    .tk-tablo td.s, .tk-tablo th.s {
      text-align: right;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    .tk-tablo td.pay {
      min-width: 90px;
    }
    .tk-tablo td.baslik {
      max-width: 380px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tk-nokta {
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 2px;
      margin-right: 6px;
      vertical-align: baseline;
    }
    .tk-grafik {
      display: flex;
      align-items: flex-end;
      gap: 3px;
      height: 170px;
      padding-top: 18px;
      border-bottom: 1px solid var(--border);
    }
    .tk-sutun {
      flex: 1;
      min-width: 6px;
      height: 100%;
      display: flex;
      flex-direction: column-reverse;
      position: relative;
      cursor: default;
    }
    .tk-sutun i {
      display: block;
      width: 100%;
    }
    .tk-sutun i:last-child {
      border-radius: 3px 3px 0 0;
    }
    .tk-sutun:hover {
      filter: brightness(1.18);
    }
    .tk-eksen {
      display: flex;
      gap: 3px;
      margin-top: 3px;
    }
    .tk-eksen span {
      flex: 1;
      min-width: 6px;
      text-align: center;
      font-size: 10px;
      color: var(--muted);
      overflow: hidden;
      white-space: nowrap;
    }
    .tk-lejant {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      font-size: 12px;
      color: var(--muted);
      margin-top: 8px;
    }
    .tk-izgara {
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }
    @media (max-width: 900px) {
      .tk-izgara { grid-template-columns: 1fr; }
    }
    .hm-satir {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      padding: 6px 0;
      border-bottom: 1px dashed var(--border);
      font-size: 13px;
    }
    .hm-satir:last-child {
      border-bottom: 0;
    }
    .hm-satir span:first-child {
      color: var(--muted);
    }
    .tk-not {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.5;
      margin: 4px 0 10px;
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
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <div class="nav-tabs" id="mainTabs">
          <button type="button" class="nav-tab active" data-tab="console">⚡ Görev Konsolu</button>
          <button type="button" class="nav-tab" data-tab="tokens">🔥 Token Harcama <span id="tabTokenBadge" class="badge yellow" style="display:none;font-size:11px;padding:1px 6px;"></span></button>
        </div>
        <div id="statusBadge" class="badge">Hazır</div>
      </div>
    </header>

    <div id="consolePanel" class="grid">
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
          <strong style="font-size:14px;">Geçmiş Görevler</strong>
          <button type="button" id="refreshJobsBtn" class="chip">Yenile</button>
        </div>
        <input type="search" id="jobSearch" placeholder="Görev ara… (metin, emir106, claude, done)" autocomplete="off" />
        <div id="jobList" class="job-list">
          <div class="sub">Yükleniyor...</div>
        </div>
      </div>

      <div class="col-mid">
        <form id="runForm" class="card">
          <div>
            <label for="task">Görev / Prompt <span class="sub">(Ctrl+Enter ile gönder)</span></label>
            <textarea id="task" name="task" placeholder="Yapılacak işi yazın (örn. merhaba dünya dosyası oluştur)..." required></textarea>
          </div>

          <div class="row">
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

        <div id="resultBox" class="card">
          <div class="res-header">
            <strong style="font-size:14px;">İşlem Sonucu</strong>
            <span id="resultStatusBadge" class="badge" style="display:none;"></span>
          </div>
          <div id="resultContent">
            <div class="sub">Henüz sonuç yok.</div>
          </div>
        </div>
      </div>

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

    <section id="tokenPanel" style="display:none;">
      <div class="card" style="margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding:12px 16px;">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <strong style="font-size:15px;">🔥 Token Harcama &amp; Maliyet Defteri</strong>
          <div class="tk-donem" id="tkDonem">
            <button type="button" data-d="5s">Son 5 saat</button>
            <button type="button" data-d="bugun" class="aktif">Bugün</button>
            <button type="button" data-d="7g">7 gün</button>
            <button type="button" data-d="30g">30 gün</button>
            <button type="button" data-d="tum">Tümü</button>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;">
          <span class="badge" id="tkYas"></span>
          <button type="button" id="tkYenile" class="chip" style="padding:5px 10px;font-size:12px;">↻ Yenile</button>
        </div>
      </div>

      <div class="tk-kpi" id="tkKpi"></div>
      <div class="tk-motorlar" id="tkMotorlar"></div>

      <div class="card">
        <div class="tk-card-head">
          <h3>Model bazında yakım</h3>
          <span class="badge cyan" id="tkModelRozet"></span>
        </div>
        <div class="tk-kaydir" id="tkModeller"></div>
      </div>

      <div class="card">
        <div class="tk-card-head">
          <h3>Günlük yakım · son 30 gün</h3>
          <div class="tk-olcu" id="tkOlcu">
            <button type="button" data-o="toplam" class="aktif">Toplam token</button>
            <button type="button" data-o="cikti">Çıktı token</button>
            <button type="button" data-o="usd">API eşdeğeri $</button>
          </div>
        </div>
        <div id="tkGrafik"></div>
      </div>

      <div class="tk-izgara">
        <div>
          <div class="card">
            <div class="tk-card-head">
              <h3>Son oturumlar (Claude)</h3>
              <span class="badge" id="tkOturumRozet"></span>
            </div>
            <div class="tk-kaydir" id="tkOturumlar"></div>
          </div>
        </div>
        <div>
          <div class="card">
            <div class="tk-card-head">
              <h3>Kaynak / proje</h3>
            </div>
            <div class="tk-kaydir" id="tkKaynaklar"></div>
          </div>
          <div class="card">
            <div class="tk-card-head">
              <h3>Antigravity (agy) kotası</h3>
            </div>
            <div id="tkKota"></div>
          </div>
        </div>
      </div>

      <p class="tk-not" id="tkNot"></p>
    </section>
  </div>

  <script>
    const JRC_TOKEN = __JRC_TOKEN_JSON__;
    const JRC_UI_VER = 5;

    const taskEl = document.getElementById('task');
    const toolEl = document.getElementById('tool');
    const dryRunEl = document.getElementById('dryRun');
    const submitBtn = document.getElementById('submitBtn');
    const outputEl = document.getElementById('output');
    const decisionBox = document.getElementById('decisionBox');
    const decisionGrid = document.getElementById('decisionGrid');
    const statusBadge = document.getElementById('statusBadge');
    const jobTimer = document.getElementById('jobTimer');
    const jobListEl = document.getElementById('jobList');
    const jobSearchEl = document.getElementById('jobSearch');
    const resultStatusBadge = document.getElementById('resultStatusBadge');
    const resultContent = document.getElementById('resultContent');

    let activeJobId = null;
    let pollTimer = null;
    let startTimeMs = null;
    let allJobs = [];

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

    function extractResultText(data) {
      if (typeof data.result_output === 'string' && data.result_output.trim() !== '') {
        return data.result_output.trim();
      }
      if (data.status === 'queued') {
        return `🕒 Sırada bekliyor (#${data.queue_pos || '?'}). Önceki görev bitince otomatik başlayacak.`;
      }
      const raw = (data.output || '').trim();
      if (data.status === 'cancelled' && !raw) {
        return '⊘ Sıradan çıkarıldı (iptal).';
      }
      if (data.status === 'running') {
        if (raw.includes('iş emri hazırlanıyor')) {
          return '⏳ İş emri hazırlandı, yürütücü ajan çalışıyor...';
        }
        if (!raw) {
          return '⏳ Çıktı bekleniyor...';
        }
      }
      if (!raw) {
        return '(Çıktı yok)';
      }
      if (data.dry_run) {
        return raw;
      }
      const cmdMatch = raw.match(/^\\s*└─ Komut\\s*:[^\\n]*\\n+/m);
      if (cmdMatch) {
        const idx = cmdMatch.index + cmdMatch[0].length;
        const tail = raw.slice(idx).trim();
        if (tail) {
          const tailLines = tail.split('\\n');
          return tailLines.length > 40 ? tailLines.slice(-40).join('\\n') : tail;
        }
      }
      const lines = raw.split('\\n');
      return lines.slice(-40).join('\\n');
    }

    function classifyResultLine(line) {
      const t = line.trim();
      if (!t) return 'res-empty';
      if (
        t.includes('✗') ||
        /^(traceback\\b|error:|hata:|exception\\b)/i.test(t) ||
        /\\b(traceback|syntaxerror|typeerror|valueerror|runtimeerror)\\b/i.test(t)
      ) {
        return 'res-red';
      }
      if (t.includes('✓') || t.includes('✅')) {
        return 'res-green';
      }
      if (t.includes('⚠') || t.includes('⊘') || /(^|\\s)(uyarı|warning)(\\s|:|,|\\.|$)/i.test(t)) {
        return 'res-yellow';
      }
      if (t.includes('├─') || t.includes('└─') || t.startsWith('│') || t.startsWith('🎬')) {
        return 'res-muted';
      }
      if (
        !/(0\\s+(js\\s+)?hata|hatasız|no\\s+error|0\\s+error)/i.test(t) &&
        /(^|\\s)(hata|error|başarısız)(\\s|:|,|\\.|$)/i.test(t)
      ) {
        return 'res-red';
      }
      if (/(^|\\s)(başarılı|başarı|tamamlandı)(\\s|:|,|\\.|!|$)/i.test(t)) {
        return 'res-green';
      }
      if (/^#{1,4}\\s+/.test(t) || t.startsWith('⚡')) {
        return 'res-heading';
      }
      return '';
    }

    function renderResult(data) {
      if (!data) {
        resultStatusBadge.style.display = 'none';
        const empty = document.createElement('div');
        empty.className = 'sub';
        empty.textContent = 'Henüz sonuç yok.';
        resultContent.replaceChildren(empty);
        return;
      }

      resultStatusBadge.style.display = 'inline-flex';
      if (data.status === 'done') {
        resultStatusBadge.className = 'badge green';
        resultStatusBadge.textContent = '✓ Tamamlandı';
      } else if (data.status === 'error') {
        resultStatusBadge.className = 'badge red';
        resultStatusBadge.textContent = `✗ Hata (kod: ${data.exit_code ?? 1})`;
      } else if (data.status === 'running') {
        resultStatusBadge.className = 'badge yellow';
        resultStatusBadge.textContent = '⏳ Çalışıyor';
      } else if (data.status === 'queued') {
        resultStatusBadge.className = 'badge purple';
        resultStatusBadge.textContent = data.queue_pos ? `🕒 Sırada (#${data.queue_pos})` : '🕒 Sırada';
      } else if (data.status === 'cancelled') {
        resultStatusBadge.className = 'badge';
        resultStatusBadge.textContent = '⊘ İptal edildi';
      } else {
        resultStatusBadge.className = 'badge';
        resultStatusBadge.textContent = data.status || 'Hazır';
      }

      const frag = document.createDocumentFragment();

      const taskDiv = document.createElement('div');
      taskDiv.className = 'res-task';
      if (data.no) {
        const emirSpan = document.createElement('span');
        emirSpan.className = 'emir-tag';
        emirSpan.textContent = `emir${data.no}`;
        taskDiv.appendChild(emirSpan);
      }
      taskDiv.appendChild(document.createTextNode((data.dry_run ? '[DRY-RUN] ' : '') + (data.task || '—')));
      frag.appendChild(taskDiv);

      const metaDiv = document.createElement('div');
      metaDiv.className = 'res-meta';
      const dec = data.decision;
      const modelTag = dec ? `${dec.tool}:${dec.model}` : (data.tool || 'auto');
      const modelCls = dec ? (dec.tool === 'claude' ? 'green' : 'cyan') : '';
      const exitDefined = data.exit_code !== null && data.exit_code !== undefined;
      const exitCls = exitDefined ? (data.exit_code === 0 ? 'green' : 'red') : '';
      const metaBadges = [
        { text: modelTag, cls: modelCls },
        { text: exitDefined ? `Çıkış: ${data.exit_code}` : 'Çıkış: —', cls: exitCls },
      ];
      if (data.created_at) {
        metaBadges.push({ text: `Oluşturma: ${data.created_at}`, cls: '' });
      }
      if (data.finished_at) {
        metaBadges.push({ text: `Bitiş: ${data.finished_at}`, cls: '' });
      }
      for (const b of metaBadges) {
        const sp = document.createElement('span');
        sp.className = 'badge' + (b.cls ? ' ' + b.cls : '');
        sp.textContent = b.text;
        metaDiv.appendChild(sp);
      }
      frag.appendChild(metaDiv);

      const bodyDiv = document.createElement('div');
      bodyDiv.className = 'res-body';
      const resultText = extractResultText(data);
      const lines = resultText.split('\\n');
      for (const line of lines) {
        const lineEl = document.createElement('div');
        const cls = classifyResultLine(line);
        lineEl.className = 'res-line' + (cls ? ' ' + cls : '');
        if (cls !== 'res-empty') {
          lineEl.textContent = line;
        }
        bodyDiv.appendChild(lineEl);
      }
      frag.appendChild(bodyDiv);

      const imgPaths = extractImagePaths(resultText);
      if (imgPaths.length) {
        const galleryDiv = document.createElement('div');
        galleryDiv.className = 'res-gallery';
        for (const imgPath of imgPaths) {
          const cardBtn = document.createElement('button');
          cardBtn.type = 'button';
          cardBtn.className = 'res-thumb-card';
          cardBtn.title = imgPath + ' (Büyütmek için tıklayın)';

          const imgUrl = '/api/image?path=' + encodeURIComponent(imgPath);
          const fileName = imgPath.split('/').pop() || imgPath;

          const imgEl = document.createElement('img');
          imgEl.className = 'res-thumb-img';
          imgEl.src = imgUrl;
          imgEl.alt = fileName;
          imgEl.loading = 'lazy';
          imgEl.addEventListener('error', () => {
            cardBtn.remove();
            if (!galleryDiv.childElementCount) {
              galleryDiv.remove();
            }
          });

          const capEl = document.createElement('span');
          capEl.className = 'res-thumb-cap';
          capEl.textContent = '🔍 ' + fileName;

          cardBtn.appendChild(imgEl);
          cardBtn.appendChild(capEl);
          cardBtn.addEventListener('click', () => openImageLightbox(imgUrl, imgPath));
          galleryDiv.appendChild(cardBtn);
        }
        frag.appendChild(galleryDiv);
      }

      const pdfPaths = extractPdfPaths(resultText);
      if (pdfPaths.length) {
        const filesDiv = document.createElement('div');
        filesDiv.className = 'res-files';
        for (const pdfPath of pdfPaths) {
          const rowDiv = document.createElement('div');
          rowDiv.className = 'res-file-row';
          rowDiv.title = pdfPath;

          const fileName = pdfPath.split('/').pop() || pdfPath;
          const nameSpan = document.createElement('span');
          nameSpan.className = 'res-file-name';
          nameSpan.title = pdfPath;
          nameSpan.textContent = '📄 ' + fileName;

          const actionsDiv = document.createElement('div');
          actionsDiv.className = 'res-file-actions';

          const encodedPath = encodeURIComponent(pdfPath);
          const openLink = document.createElement('a');
          openLink.className = 'res-file-btn primary';
          openLink.target = '_blank';
          openLink.rel = 'noopener';
          openLink.href = '/api/file?path=' + encodedPath;
          openLink.textContent = 'Aç';

          const dlLink = document.createElement('a');
          dlLink.className = 'res-file-btn';
          dlLink.href = '/api/file?path=' + encodedPath + '&download=1';
          dlLink.setAttribute('download', fileName);
          dlLink.textContent = 'İndir';

          actionsDiv.appendChild(openLink);
          actionsDiv.appendChild(dlLink);
          rowDiv.appendChild(nameSpan);
          rowDiv.appendChild(actionsDiv);
          filesDiv.appendChild(rowDiv);
        }
        frag.appendChild(filesDiv);
      }

      resultContent.replaceChildren(frag);
    }

    function extractImagePaths(text) {
      if (!text) return [];
      const re = /(?:^|[\\s"'`([<=]|file:\\/\\/)(\\/[A-Za-z0-9_][^\\s"'`<>()\\[\\]*?]*\\.(?:png|jpe?g|webp|gif))/gi;
      const seen = new Set();
      const paths = [];
      let m;
      while ((m = re.exec(text)) !== null) {
        let p = m[1];
        try { p = decodeURIComponent(p); } catch (e) {}
        if (!seen.has(p)) {
          seen.add(p);
          paths.push(p);
        }
      }
      return paths;
    }

    function extractPdfPaths(text) {
      if (!text) return [];
      const re = /(?:^|[\\s"'`([<=]|file:\\/\\/)(\\/[A-Za-z0-9_][^\\s"'`<>()\\[\\]*?]*\\.pdf)/gi;
      const seen = new Set();
      const paths = [];
      let m;
      while ((m = re.exec(text)) !== null) {
        let p = m[1];
        try { p = decodeURIComponent(p); } catch (e) {}
        if (!seen.has(p)) {
          seen.add(p);
          paths.push(p);
        }
      }
      return paths;
    }

    let activeLightboxEl = null;

    function closeImageLightbox() {
      if (activeLightboxEl) {
        activeLightboxEl.remove();
        activeLightboxEl = null;
      }
    }

    function openImageLightbox(imgUrl, filePath) {
      closeImageLightbox();
      const overlay = document.createElement('div');
      overlay.className = 'img-lightbox';
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');

      const closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.className = 'img-lightbox-close';
      closeBtn.textContent = '✕ Kapat (ESC)';
      closeBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        closeImageLightbox();
      });

      const fullImg = document.createElement('img');
      fullImg.className = 'img-lightbox-img';
      fullImg.src = imgUrl;
      fullImg.alt = filePath.split('/').pop() || filePath;
      fullImg.addEventListener('click', (ev) => ev.stopPropagation());

      const cap = document.createElement('div');
      cap.className = 'img-lightbox-cap';
      cap.textContent = filePath;
      cap.addEventListener('click', (ev) => ev.stopPropagation());

      overlay.appendChild(closeBtn);
      overlay.appendChild(fullImg);
      overlay.appendChild(cap);
      overlay.addEventListener('click', (ev) => {
        if (ev.target === overlay) {
          closeImageLightbox();
        }
      });

      document.body.appendChild(overlay);
      activeLightboxEl = overlay;
    }

    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && activeLightboxEl) {
        closeImageLightbox();
      }
    });

    async function pollJob(jobId) {
      if (pollTimer) clearTimeout(pollTimer);
      try {
        const res = await fetch(`/api/job/${encodeURIComponent(jobId)}`, {
          headers: { 'X-JRC-Token': JRC_TOKEN }
        });
        const data = await res.json();
        if (activeJobId && jobId !== activeJobId) return;
        if (!data.ok) {
          outputEl.textContent = 'Hata: ' + (data.error || 'İş okunamadı');
          submitBtn.disabled = false;
          return;
        }
        renderDecision(data.decision, data.dry_run);
        renderResult(data);
        if (data.ui_ver && data.ui_ver !== JRC_UI_VER) {
          location.reload();
          return;
        }
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

    function renderJobs() {
      if (!allJobs.length) {
        jobListEl.innerHTML = '<div class="sub">Henüz kayıtlı görev yok.</div>';
        return;
      }
      const q = (jobSearchEl ? jobSearchEl.value : '').trim().toLocaleLowerCase('tr');
      const filtered = q ? allJobs.filter((j) => {
        const emir = j.no ? ('emir' + j.no) : '';
        const decTool = j.decision && j.decision.tool ? j.decision.tool : (j.tool || '');
        const decModel = j.decision && j.decision.model ? j.decision.model : '';
        const haystack = [
          j.task || '',
          emir,
          decTool,
          decModel,
          decModel ? (decTool + ':' + decModel) : '',
          j.status || '',
          j.created_at || ''
        ].join(' ').toLocaleLowerCase('tr');
        return haystack.includes(q);
      }) : allJobs;

      if (!filtered.length) {
        jobListEl.innerHTML = '<div class="sub">Eşleşen görev yok.</div>';
        return;
      }

      jobListEl.innerHTML = '';
      for (const j of filtered) {
        const div = document.createElement('div');
        div.className = 'job-item' + (j.id === activeJobId ? ' active' : '');
        const tDiv = document.createElement('div');
        tDiv.className = 'job-task';
        if (j.no) {
          const emirSpan = document.createElement('span');
          emirSpan.className = 'emir-tag';
          emirSpan.textContent = `emir${j.no}`;
          tDiv.appendChild(emirSpan);
        }
        tDiv.appendChild(document.createTextNode((j.dry_run ? '[DRY] ' : '') + (j.task || '')));
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
          if (j.no) {
            const tag = 'emir' + j.no;
            const v = taskEl.value;
            if (!new RegExp('\\\\b' + tag + '\\\\b', 'i').test(v)) {
              taskEl.value = v + (v && !/\\s$/.test(v) ? ' ' : '') + tag + ' ';
            }
            taskEl.focus();
            const len = taskEl.value.length;
            taskEl.setSelectionRange(len, len);
          }
          activeJobId = j.id;
          startTimeMs = null;
          pollJob(j.id);
          loadJobs();
        });
        jobListEl.appendChild(div);
      }
    }

    if (jobSearchEl) {
      jobSearchEl.addEventListener('input', renderJobs);
      jobSearchEl.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          e.preventDefault();
          jobSearchEl.value = '';
          renderJobs();
        }
      });
    }

    async function loadJobs() {
      try {
        const res = await fetch('/api/jobs', {
          headers: { 'X-JRC-Token': JRC_TOKEN }
        });
        const data = await res.json();
        if (data.ui_ver && data.ui_ver !== JRC_UI_VER) {
          location.reload();
          return;
        }
        allJobs = data.jobs || [];
        if (!activeJobId && allJobs[0] && allJobs[0].id) {
          activeJobId = allJobs[0].id;
          pollJob(activeJobId);
        }
        renderJobs();
      } catch (e) {
        jobListEl.innerHTML = '<div class="sub">Geçmiş yüklenemedi.</div>';
      }
    }

    document.getElementById('refreshJobsBtn').addEventListener('click', loadJobs);

    document.getElementById('runForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const task = taskEl.value.trim();
      if (!task) return;

      submitBtn.disabled = true;
      setStatus('running');
      decisionBox.style.display = 'none';
      outputEl.textContent = 'Görev başlatılıyor...';
      renderResult({
        status: 'running',
        no: null,
        task: task,
        tool: toolEl.value,
        dry_run: dryRunEl.checked,
        output: '⏳ Görev başlatılıyor...'
      });
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
            tool: toolEl.value,
            dry_run: dryRunEl.checked
          })
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
          const errMsg = '✗ Hata: ' + (data.error || res.statusText);
          outputEl.textContent = errMsg;
          setStatus('error', 1);
          renderResult({
            status: 'error',
            task: task,
            tool: toolEl.value,
            dry_run: dryRunEl.checked,
            exit_code: 1,
            result_output: errMsg
          });
          submitBtn.disabled = false;
          return;
        }
        activeJobId = data.id;
        submitBtn.disabled = false;
        if (data.no) {
          renderResult({
            status: data.status || 'running',
            no: data.no,
            queue_pos: data.queue_pos,
            task: task,
            tool: toolEl.value,
            dry_run: dryRunEl.checked,
            output: data.status === 'queued'
              ? `🕒 Sırada bekliyor (#${data.queue_pos || '?'}). Önceki görev bitince otomatik başlayacak.`
              : '⏳ Görev başlatılıyor...'
          });
        }
        if (data.status === 'queued') {
          startTimeMs = null;
          jobTimer.textContent = '';
          taskEl.value = '';
        }
        loadJobs();
        pollJob(data.id);
      } catch (err) {
        const errMsg = '✗ İstek hatası: ' + err;
        outputEl.textContent = errMsg;
        setStatus('error', 1);
        renderResult({
          status: 'error',
          task: task,
          tool: toolEl.value,
          dry_run: dryRunEl.checked,
          exit_code: 1,
          result_output: errMsg
        });
        submitBtn.disabled = false;
      }
    });

    loadJobs();
    setInterval(loadJobs, 5000);

    // ── Token Harcama Sekmesi ───────────────────────────────────────────────
    function esc(s) {
      return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    let tkVeri = null, tkDonem = 'bugun', tkOlcu = 'toplam';
    const TK_RENK = ['#3b82f6', '#e07a1f', '#1e9e4a', '#a855f7', '#ef4444', '#06b6d4', '#eab308'];
    const TK_DIGER = '#8b949e';
    const TK_MOTOR = { claude: 'Claude Code', agy: 'Antigravity (agy)', api: 'Doğrudan API' };
    const TK_DONEM = { '5s': 'son 5 saat', bugun: 'bugün', '7g': 'son 7 gün', '30g': 'son 30 gün', tum: 'tüm kayıt' };

    function tkSayi(n) {
      n = Number(n || 0);
      for (const [s, e] of [[1e9, ' Mr'], [1e6, ' M'], [1e3, ' B']]) {
        if (n >= s) {
          return (n / s).toLocaleString('tr-TR', {
            maximumFractionDigits: n / s >= 100 ? 0 : n / s >= 10 ? 1 : 2
          }) + e;
        }
      }
      return String(Math.round(n));
    }
    const tkUsd = n => '$' + Number(n || 0).toLocaleString('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const tkTam = n => Number(n || 0).toLocaleString('tr-TR');

    function tkRenkler() {
      const buyuk = l => (l || []).slice().sort((a, b) => b.toplam - a.toplam);
      const p30 = (tkVeri.pencereler && tkVeri.pencereler['30g'] && tkVeri.pencereler['30g'].model) || [];
      const ptum = (tkVeri.pencereler && tkVeri.pencereler.tum && tkVeri.pencereler.tum.model) || [];
      const sira = buyuk(p30).concat(buyuk(ptum)).map(m => m.model);
      const r = {};
      let i = 0;
      for (const m of sira) {
        if (!(m in r)) r[m] = i < TK_RENK.length ? TK_RENK[i++] : TK_DIGER;
      }
      return r;
    }

    function tokenCiz() {
      if (!tkVeri || !tkVeri.pencereler) return;
      const v = tkVeri, w = v.pencereler[tkDonem], t = w.toplam, renk = tkRenkler();
      const yasEl = document.getElementById('tkYas');
      yasEl.textContent = (v.yas_dk < 1 ? 'az önce güncellendi' : `${v.yas_dk} dk önce güncellendi`) + (v.ilk_kayit ? ` · ilk kayıt ${v.ilk_kayit}` : '');
      yasEl.className = 'badge' + (v.yas_dk > 20 ? ' yellow' : ' green');

      const kpi = (ad, deger, alt) => `<div class="kutu"><small>${ad}</small><b>${deger}</b>${alt ? `<small>${alt}</small>` : ''}</div>`;
      document.getElementById('tkKpi').innerHTML =
        kpi('Toplam token', tkSayi(t.toplam), tkTam(t.toplam)) +
        kpi('Çıktı', tkSayi(t.cikti), 'üretilen') +
        kpi('Girdi', tkSayi(t.girdi), 'önbelleksiz') +
        kpi('Önbellek okuma', tkSayi(t.onbellek_okuma), 'ucuz tekrar okuma') +
        kpi('Önbellek yazma', tkSayi(t.onbellek_yazma), '') +
        kpi('Çağrı', tkTam(t.cagri), 'API cevabı') +
        kpi('API eşdeğeri', tkUsd(t.usd), 'liste fiyatıyla');

      const pay = (n, c) => `<div class="tk-pay"><i style="width:${t.toplam ? Math.max(1, n / t.toplam * 100).toFixed(1) : 0}%${c ? ';background:' + c : ''}"></i></div>`;
      document.getElementById('tkMotorlar').innerHTML = ['claude', 'agy', 'api'].map(k => {
        const m = w.motor[k] || { toplam: 0, cikti: 0, cagri: 0, usd: 0 };
        return `<div class="tk-motor"><h4><span>${TK_MOTOR[k]}</span><span>%${t.toplam ? (m.toplam / t.toplam * 100).toLocaleString('tr-TR', { maximumFractionDigits: 1 }) : 0}</span></h4>
          <b>${tkSayi(m.toplam)}</b>${pay(m.toplam)}<small>çıktı ${tkSayi(m.cikti)} · ${tkTam(m.cagri)} çağrı · ${k === 'agy' ? 'abonelik (fiyat yok)' : tkUsd(m.usd)}</small></div>`;
      }).join('');

      document.getElementById('tkModelRozet').textContent = TK_DONEM[tkDonem];
      document.getElementById('tkModeller').innerHTML = w.model.length
        ? `<table class="tk-tablo"><thead><tr><th>Model</th><th>Motor</th><th class="s">Çağrı</th><th class="s">Girdi</th><th class="s">Çıktı</th><th class="s">Önb. okuma</th><th class="s">Önb. yazma</th><th class="s">Toplam</th><th>Pay</th><th class="s">API eşd.</th></tr></thead><tbody>` +
          w.model.map(m => `<tr><td><i class="tk-nokta" style="background:${renk[m.model] || TK_DIGER}"></i><b>${esc(m.model)}</b></td><td>${esc(m.motor)}</td><td class="s">${tkTam(m.cagri)}</td><td class="s">${tkSayi(m.girdi)}</td><td class="s">${tkSayi(m.cikti)}</td><td class="s">${tkSayi(m.onbellek_okuma)}</td><td class="s">${tkSayi(m.onbellek_yazma)}</td><td class="s"><b>${tkSayi(m.toplam)}</b></td><td class="pay">${pay(m.toplam, renk[m.model])}</td><td class="s">${m.motor === 'agy' ? '–' : tkUsd(m.usd)}</td></tr>`).join('') +
          '</tbody></table>'
        : '<div class="sub">Bu dönemde kayıt yok.</div>';

      const gunler = v.gunluk || [], deger = x => Number(x[tkOlcu] || 0), yaz = n => tkOlcu === 'usd' ? tkUsd(n) : tkSayi(n);
      const gunToplam = g => Object.values(g.modeller).reduce((a, x) => a + deger(x), 0);
      const tepe = Math.max(...gunler.map(gunToplam), 0);
      const gorunen = new Set();
      document.getElementById('tkGrafik').innerHTML = tepe
        ? `<div class="tk-grafik">` + gunler.map(g => {
            const top = gunToplam(g), parcalar = Object.entries(g.modeller).filter(([, x]) => deger(x) > 0).sort((a, b) => deger(b[1]) - deger(a[1]));
            parcalar.forEach(([m]) => gorunen.add(m));
            const ipucu = `${g.gun.slice(8)}.${g.gun.slice(5, 7)} · ${yaz(top)}\\n` + parcalar.map(([m, x]) => `${m}: ${yaz(deger(x))}`).join('\\n');
            return `<div class="tk-sutun" title="${esc(ipucu)}">` + parcalar.map(([m, x]) => `<i style="height:${(deger(x) / tepe * 100).toFixed(2)}%;background:${renk[m] || TK_DIGER}"></i>`).join('') + '</div>';
          }).join('') +
          `</div><div class="tk-eksen">` + gunler.map((g, i) => `<span>${(gunler.length - 1 - i) % 5 === 0 ? g.gun.slice(8) + '.' + g.gun.slice(5, 7) : ''}</span>`).join('') + `</div>` +
          `<div class="tk-lejant"><span>En yüksek gün: <b>${yaz(tepe)}</b></span>` + Object.keys(renk).filter(m => gorunen.has(m)).map(m => `<span><i class="tk-nokta" style="background:${renk[m]}"></i>${esc(m)}</span>`).join('') + '</div>'
        : '<div class="sub">Son 30 günde kayıt yok.</div>';

      document.getElementById('tkKaynaklar').innerHTML = w.kaynak.length
        ? `<table class="tk-tablo"><thead><tr><th>Kaynak</th><th>Motor</th><th class="s">Toplam</th><th class="s">Çıktı</th><th class="s">API eşd.</th></tr></thead><tbody>` +
          w.kaynak.slice(0, 15).map(k => `<tr><td><b>${esc(k.kaynak)}</b></td><td>${esc(k.motor)}</td><td class="s">${tkSayi(k.toplam)}</td><td class="s">${tkSayi(k.cikti)}</td><td class="s">${k.motor === 'agy' ? '–' : tkUsd(k.usd)}</td></tr>`).join('') + '</tbody></table>'
        : '<div class="sub">Kayıt yok.</div>';

      const o = v.oturumlar || [];
      document.getElementById('tkOturumRozet').textContent = `son ${o.length}`;
      document.getElementById('tkOturumlar').innerHTML = o.length
        ? `<table class="tk-tablo"><thead><tr><th>Zaman</th><th>Emir / oturum</th><th>Proje</th><th>Model</th><th class="s">Çağrı</th><th class="s">Toplam</th><th class="s">Çıktı</th><th class="s">API eşd.</th></tr></thead><tbody>` +
          o.map(x => `<tr><td style="white-space:nowrap">${esc(x.basla)}${x.sure_dk ? ` <small>· ${x.sure_dk} dk</small>` : ''}</td><td class="baslik" title="${esc(x.baslik || '')}">${esc(x.baslik || '(' + x.oturum + ')')}</td><td>${esc(x.proje)}</td><td>${x.modeller.map(m => `<i class="tk-nokta" style="background:${renk[m] || TK_DIGER}" title="${esc(m)}"></i>`).join('')}${esc(x.modeller.map(m => m.replace(/^claude-/, '')).join(', '))}</td><td class="s">${tkTam(x.cagri)}</td><td class="s"><b>${tkSayi(x.toplam)}</b></td><td class="s">${tkSayi(x.cikti)}</td><td class="s">${tkUsd(x.usd)}</td></tr>`).join('') +
          '</tbody></table>'
        : '<div class="sub">Oturum yok.</div>';

      const q = v.agy_kota;
      document.getElementById('tkKota').innerHTML = q && (q.gruplar || []).length
        ? q.gruplar.map(g => `<div style="font-weight:700;font-size:13px;margin:4px 0">${esc(g.ad || '')}</div>` + g.kovalar.map(b => {
            const kalan = Math.round((b.kalan || 0) * 100);
            return `<div class="hm-satir"><span>${esc((b.ad || '').replace(' Remaining', '').replace('Weekly Limit', 'Haftalık').replace('Five Hour Limit', '5 saatlik'))}</span><b>%${kalan} kaldı</b></div><div class="tk-pay"><i style="width:${kalan}%;background:${kalan < 20 ? 'var(--red)' : 'var(--green)'}"></i></div>`;
          }).join('')).join('') + `<small class="sub">ölçüm: ${new Date(q.zaman * 1000).toLocaleString('tr-TR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })} (jrc usage)</small>`
        : '<div class="sub">Kota ölçümü yok (jrc usage).</div>';

      document.getElementById('tkNot').innerHTML =
        'Kayıt: Claude Code oturumlarının gerçek <code>usage</code> sayaçları (jrc, botlar, elle açılan tüm oturumlar) + maliyet defterindeki agy ve doğrudan API çağrıları. ' +
        'Kalıcı defter bu bilgisayarda <code>~/.cache/jroute/tokens.db</code>; 5 dakikada bir ve her jrc görevinden sonra güncellenir. ' +
        '<b>API eşdeğeri</b> liste fiyatıyla hesaptır, Claude aboneliğinde ayrıca ödenmez; agy için liste fiyatı yoktur. ' +
        "agy'de yalnız token sayacı dönen çağrılar (Telegram ajanı, proxy) görünür; terminalde etkileşimli agy oturumları sayaç vermez.";
    }

    async function tokenGetir(refresh = false) {
      try {
        const res = await fetch(refresh ? '/api/tokens?refresh=1' : '/api/tokens', {
          headers: { 'X-JRC-Token': JRC_TOKEN }
        });
        const v = await res.json();
        if (!res.ok || !v.zaman) {
          document.getElementById('tkKpi').innerHTML = '<div class="sub">Token defteri henüz yazılmadı (jrc tokens).</div>';
          return;
        }
        tkVeri = v;
        tokenCiz();
        const bTop = v.pencereler && v.pencereler.bugun && v.pencereler.bugun.toplam;
        const badgeEl = document.getElementById('tabTokenBadge');
        if (bTop && badgeEl) {
          badgeEl.style.display = 'inline-flex';
          badgeEl.textContent = `${tkSayi(bTop.toplam)} · ${tkUsd(bTop.usd)}`;
        }
      } catch (e) {
        document.getElementById('tkKpi').innerHTML = '<div class="sub">Token verisi okunamadı.</div>';
      }
    }

    document.querySelectorAll('#tkDonem button').forEach(b => b.addEventListener('click', () => {
      tkDonem = b.dataset.d;
      document.querySelectorAll('#tkDonem button').forEach(x => x.classList.toggle('aktif', x === b));
      if (tkVeri) tokenCiz();
    }));

    document.querySelectorAll('#tkOlcu button').forEach(b => b.addEventListener('click', () => {
      tkOlcu = b.dataset.o;
      document.querySelectorAll('#tkOlcu button').forEach(x => x.classList.toggle('aktif', x === b));
      if (tkVeri) tokenCiz();
    }));

    document.getElementById('tkYenile').addEventListener('click', () => tokenGetir(true));

    function switchTab(tab, updateHash = true) {
      const isTokens = tab === 'tokens';
      document.querySelectorAll('#mainTabs .nav-tab').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === (isTokens ? 'tokens' : 'console'));
      });
      document.getElementById('consolePanel').style.display = isTokens ? 'none' : '';
      document.getElementById('tokenPanel').style.display = isTokens ? 'block' : 'none';
      if (updateHash) {
        const h = isTokens ? '#tokens' : '';
        if (location.hash !== h) {
          history.replaceState(null, '', location.pathname + h);
        }
      }
      if (isTokens) tokenGetir(false);
    }

    document.querySelectorAll('#mainTabs .nav-tab').forEach(b => {
      b.addEventListener('click', () => switchTab(b.dataset.tab, true));
    });

    function syncHashTab() {
      const h = decodeURIComponent(location.hash.replace(/^#\\/?/, '')).toLowerCase();
      if (h === 'tokens' || h === 'token' || h === 'hermes-token') {
        switchTab('tokens', false);
      } else if (h === 'console' || h === '') {
        switchTab('console', false);
      }
    }
    window.addEventListener('hashchange', syncHashTab);
    tokenGetir(false);
    syncHashTab();
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
            html = HTML_TEMPLATE.replace("__JRC_TOKEN_JSON__", json.dumps(tok))
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

        if path == "/api/image":
            qs = urllib.parse.parse_qs(parsed.query)
            raw_img_path = (qs.get("path", [""])[0] or "").strip()
            if raw_img_path.startswith("file://"):
                raw_img_path = urllib.parse.unquote(raw_img_path[7:])
            if not raw_img_path or not raw_img_path.startswith("/"):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Geçersiz görsel yolu."})
                return

            real_target = os.path.realpath(raw_img_path)
            allowed_reals = [os.path.realpath(r) for r in IMAGE_ALLOWED_ROOTS if r]
            in_allowed = False
            for root_real in allowed_reals:
                try:
                    if os.path.commonpath([real_target, root_real]) == root_real and real_target != root_real:
                        in_allowed = True
                        break
                except ValueError:
                    continue
            if not in_allowed:
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "İzinli kök dizin dışında erişim engellendi."})
                return

            ext = os.path.splitext(real_target)[1].lower()
            mime = IMAGE_MIME_TYPES.get(ext)
            if not mime:
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Yalnızca görsel uzantılarına (png/jpg/jpeg/webp/gif) izin verilir."})
                return

            if not os.path.isfile(real_target):
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Görsel dosyası bulunamadı."})
                return

            try:
                img_bytes = Path(real_target).read_bytes()
            except OSError:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Görsel okunamadı."})
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(img_bytes)))
            self.send_header("Cache-Control", "private, max-age=5")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(img_bytes)
            return

        if path == "/api/file":
            qs = urllib.parse.parse_qs(parsed.query)
            raw_file_path = (qs.get("path", [""])[0] or "").strip()
            if raw_file_path.startswith("file://"):
                raw_file_path = urllib.parse.unquote(raw_file_path[7:])
            if not raw_file_path or not raw_file_path.startswith("/"):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Geçersiz dosya yolu."})
                return

            real_target = os.path.realpath(raw_file_path)
            allowed_reals = [os.path.realpath(r) for r in IMAGE_ALLOWED_ROOTS if r]
            in_allowed = False
            for root_real in allowed_reals:
                try:
                    if os.path.commonpath([real_target, root_real]) == root_real and real_target != root_real:
                        in_allowed = True
                        break
                except ValueError:
                    continue
            if not in_allowed:
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "İzinli kök dizin dışında erişim engellendi."})
                return

            ext = os.path.splitext(real_target)[1].lower()
            if ext != ".pdf":
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Yalnızca .pdf uzantısına izin verilir."})
                return

            if not os.path.isfile(real_target):
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "PDF dosyası bulunamadı."})
                return

            try:
                file_bytes = Path(real_target).read_bytes()
            except OSError:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Dosya okunamadı."})
                return

            orig_name = os.path.basename(real_target) or "dosya.pdf"
            ascii_name = orig_name.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU"))
            ascii_name = re.sub(r"[^\x20-\x7e]", "_", ascii_name).replace('"', "_").replace("\\", "_").strip() or "dosya.pdf"
            utf8_name = urllib.parse.quote(orig_name, safe="")
            is_download = qs.get("download", ["0"])[0] in ("1", "true", "yes")
            disp_type = "attachment" if is_download else "inline"

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(file_bytes)))
            self.send_header(
                "Content-Disposition",
                f"{disp_type}; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}",
            )
            self.send_header("Cache-Control", "private, max-age=5")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(file_bytes)
            return

        if path == "/api/jobs":
            jobs = list_jobs(200)
            pos = _queue_positions()
            for j in jobs:
                if j.get("id") in pos:
                    j["queue_pos"] = pos[j["id"]]
            self._send_json(HTTPStatus.OK, {"ok": True, "ui_ver": 5, "jobs": jobs, "queue_len": len(pos)})
            return

        if path == "/api/tokens":
            qs = urllib.parse.parse_qs(parsed.query)
            force_refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
            veri: dict[str, Any] | None = None
            if force_refresh:
                try:
                    jrc_usage.collect_agy_usage()
                except Exception:
                    pass
                try:
                    veri = jrc_tokens.refresh()
                except Exception:
                    veri = None
            if veri is None:
                summary_path = jrc_tokens._summary_file()
                try:
                    veri = json.loads(summary_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    try:
                        veri = jrc_tokens.refresh()
                    except Exception:
                        veri = {"zaman": None}
            veri["yas_dk"] = int((time.time() - veri["zaman"]) // 60) if veri.get("zaman") else None
            veri["ok"] = True
            self._send_json(HTTPStatus.OK, veri)
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
            result_output = ""
            chunk = ""
            next_offset = offset
            if log_file.exists():
                try:
                    raw_bytes = log_file.read_bytes()
                    raw_str = raw_bytes.decode("utf-8", errors="replace")
                    full_output = strip_ansi(raw_str)
                    result_output = extract_result_section(raw_str, bool(meta.get("dry_run")))
                    chunk_bytes = raw_bytes[offset:]
                    chunk = strip_ansi(chunk_bytes.decode("utf-8", errors="replace"))
                    next_offset = len(raw_bytes)
                except OSError:
                    pass

            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "ui_ver": 5,
                    **meta,
                    "queue_pos": _queue_positions().get(job_id),
                    "output": full_output,
                    "result_output": result_output,
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

        uid_raw = data.get("user_id")
        uid = int(uid_raw) if isinstance(uid_raw, int) or (isinstance(uid_raw, str) and uid_raw.isdigit()) else None
        uname = str(data.get("user_name") or "").strip() or None
        ulogin = str(data.get("user_login") or "").strip() or None
        src = str(data.get("source") or "").strip() or None

        ok, payload = enqueue_job(
            task=str(data.get("task") or ""),
            tool=str(data.get("tool") or "auto"),
            dry_run=bool(data.get("dry_run", False)),
            user_id=uid,
            user_name=uname,
            user_login=ulogin,
            source=src,
        )
        self._send_json(HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST, payload)


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
    with _QUEUE_COND:
        backfill_emir_numbers()
    threading.Thread(target=_dispatcher_loop, name="jrc-queue", daemon=True).start()
    threading.Thread(target=_bridge_loop, name="jrc-odoo-bridge", daemon=True).start()

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
