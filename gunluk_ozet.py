#!/home/efe/Desktop/jev/projeler/odoo/.venv/bin/python
# -*- coding: utf-8 -*-
"""Peribot Günlük Çalışma ve İşlem Özeti PDF Üretici ve Gönderici.

Her gün 23:59'da (Europe/Istanbul) çalıştırılarak aynı günün 00:00–23:59
aralığındaki JRC/JEV çalışmalarını proje ve site bazında özetleyen Türkçe PDF
üretir (`artifacts/gunluk_ozet_YYYYMMDD.pdf`) ve Peribot (@teleperiBot)
üzerinden Telegram chat 900224672'ye belge olarak gönderir.

Veri kaynakları (kural tabanlı, sıfır bulut token maliyeti):
  1. /home/efe/Desktop/jev/.jrc_context.md ("Son Görevler" tablosu + "Ortak Notlar")
  2. ~/.cache/jroute/gate.jsonl (ve varsa ayrı inode'lu odoo addons .jrc_ledger.jsonl)
  3. JRC/jroute emir geçmişi:
     - /home/efe/Desktop/jev/.jrc_state.json (task_history)
     - ~/.cache/jroute/web_jobs/*.json + *.log + ~/.cache/jroute/emir_seq.json
     - ~/.cache/jroute/reports/*.md

Kullanım:
    projeler/odoo/.venv/bin/python gunluk_ozet.py
    projeler/odoo/.venv/bin/python gunluk_ozet.py --tarih 2026-11-19 --gonderme
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from fpdf import FPDF

JEV_ROOT = Path(__file__).resolve().parent
ODOO_DIR = JEV_ROOT / "projeler" / "odoo"
ARTIFACTS_DIR = JEV_ROOT / "artifacts"
CONTEXT_MD = JEV_ROOT / ".jrc_context.md"
STATE_JSON = JEV_ROOT / ".jrc_state.json"

CACHE_DIR = Path.home() / ".cache" / "jroute"
GATE_JSONL = CACHE_DIR / "gate.jsonl"
EXTRA_LEDGER_JSONL = ODOO_DIR / "addons" / ".jrc_ledger.jsonl"
WEB_JOBS_DIR = CACHE_DIR / "web_jobs"
EMIR_SEQ_JSON = CACHE_DIR / "emir_seq.json"
REPORTS_DIR = CACHE_DIR / "reports"
LOG_FILE = CACHE_DIR / "gunluk_ozet.log"

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
TARGET_CHAT_ID = 900224672
TARGET_USER = "mustafaulker34"
TARGET_BOT = "peribot"

USER_NOTES_START = "<!-- USER_NOTES_START -->"
USER_NOTES_END = "<!-- USER_NOTES_END -->"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:file|https?|conversation)://[^)]+\)")
EMIR_RE = re.compile(r"\bem(?:i|İ|ı)r\s*(\d+)\b", re.IGNORECASE)
RAW_DIRECTOR_BLOCK_RE = re.compile(
    r"(?:\x1b\[[0-9;]*m)*🎬 Yönetici \([^)]+\):(?:\x1b\[[0-9;]*m)*[^\n]*\n\x1b\[2m[\s\S]*?\x1b\[0m\r?\n*"
)
DIRECTOR_FOOTER_RE = re.compile(r"\n*🎬 Yönetici \([^)]+\):[\s\S]*$")
ROUTER_CMD_RE = re.compile(r"^\s*└─ Komut\s*:[^\n]*\n+", flags=re.MULTILINE)
NOTE_LOG_RE = re.compile(r"✓ \.jrc_context\.md notu:\s*(.+)")

ABBREV_WORDS = {
    "ör", "örn", "vb", "vs", "sat", "yak", "bkz", "no", "sn", "dk", "sa",
    "dr", "prof", "av", "st", "mr", "ms", "inc", "ltd", "aş", "şk",
}

PROJECT_GROUPS: list[tuple[str, str, tuple[tuple[str, int], ...]]] = [
    (
        "jrc",
        "JRC / JEV Router, Web Konsolu ve Altyapı",
        (
            ("jrc_web", 5), ("jrc_director", 5), ("jrc_gate", 5), ("jrc_tokens", 5),
            ("jrc_context", 5), ("jroute", 4), ("jrc ", 3), ("jrc.", 4), ("jev_cdp", 5),
            ("jev-cdp", 5), ("emir_seq", 5), ("emir numar", 5), ("start_prefetch", 5),
            ("prefetch", 4), ("gunluk_ozet", 5), ("günlük özet", 4), ("ubuntu 26.04", 4),
            ("ubuntu2604", 4), ("jrc-web", 5), ("router.py", 4), ("jroute-cli", 5),
            ("opus emri", 4), ("yönetici (opus)", 3), ("emirlerimin", 4), ("emir", 2),
        ),
    ),
    (
        "kaniskarum",
        "Kaniş Karum (kaniskarum.com ve Canlı Fiyat Takibi /live)",
        (
            ("kaniskarum", 6), ("kaniş karum", 6), ("/live", 5), ("live_members", 6),
            ("live_tracker", 6), ("live_yonetim", 6), ("scraper.html", 5),
            ("kaniskarum-scraper", 6), ("shopier", 4), ("kk_member_session", 6),
        ),
    ),
    (
        "hermes",
        "Hermes, Sosyal Medya Otomasyonu ve Peribot (OpenClaw)",
        (
            ("hermes", 5), ("sosyalmedya", 5), ("openclaw", 5), ("peribot", 4),
            ("teleperibot", 5), ("gimapp", 5), ("gunluk6", 6), ("kayseriyemek", 5),
            ("kayseritopluyemek", 6), ("yenidunyaguvenlik", 6), ("meta 190", 5),
            ("postbotu", 5), ("google flow", 5), ("peri_flow", 6), ("peri_jrc", 6),
            ("temel okul fb", 5), ("instagram", 3), ("facebook", 3),
        ),
    ),
    (
        "webler",
        "Web Siteleri (keabilgisayar, gezegengrup, kayserimimar, xaura)",
        (
            ("keabilgisayar", 6), ("catering", 5), ("kısmetim yemek", 5),
            ("gezegengrup", 5), ("gezegenmedya", 5), ("kayserimimar", 5),
            ("xaura", 6), ("gulce", 6), ("gülce", 6), ("dress-up", 6),
            ("eticaret-demo", 5), ("projeler/webler", 6), ("webler/", 5),
            ("search console", 4), ("gsc", 4), ("ai_kurul", 5), ("geocubot", 4),
        ),
    ),
    (
        "odoo",
        "Odoo, Eretna Store (eretnastore.com) ve YKS",
        (
            ("eretnastore", 6), ("eretna", 5), ("eretnabot", 5), ("odoo", 4),
            ("bizimhesap", 5), ("trendyol", 5), ("tarsus", 5), ("ptrsk", 6),
            ("bayi", 4), ("vitrin", 4), ("showcase", 5), ("foto_duzenle", 5),
            ("urun_aciklama", 5), ("vega", 4), ("earsiv", 4), ("virman", 4),
            ("eretnastore.com/yks", 6), ("projeler/yks", 6), ("yks_matematik", 6),
        ),
    ),
]


@dataclasses.dataclass
class WorkItem:
    group_key: str
    time_str: str
    sort_key: str
    title: str
    summary: str
    emir_no: int | None = None
    agent: str = ""
    open_item: str = ""


def log_line(msg: str) -> None:
    """Mesajı hem stdout'a hem de (stdout zaten log dosyasına yönlendirilmemişse) gunluk_ozet.log'a yazar."""
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        same_target = False
        if LOG_FILE.exists():
            try:
                st_out = os.fstat(sys.stdout.fileno())
                st_log = LOG_FILE.stat()
                same_target = (st_out.st_dev, st_out.st_ino) == (st_log.st_dev, st_log.st_ino)
            except OSError:
                same_target = False
        if not same_target:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def clean_markdown(text: str) -> str:
    """ANSI kodlarını, markdown link sarmallarını ve fazla boşlukları temizler."""
    if not text:
        return ""
    out = ANSI_RE.sub("", text)
    out = MD_LINK_RE.sub(r"\1", out)
    out = out.replace("`", "").replace("**", "").replace("__", "")
    out = re.sub(r"\[Hafıza:\s*\.jrc_context\.md\]\s*", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def strip_report_disclaimer(text: str) -> str:
    """Ortak not başındaki 'Rapora göre (...): ' kalıplarını özet okunaklılığı için sadeleştirir."""
    cleaned = re.sub(
        r"^Rapora göre\s*(?:\([^)]{0,120}\))?\s*[:,-]?\s*",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^Bu bilgi rapora dayanıyor,\s*[^.]+\.\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    if cleaned and cleaned[0].islower() and text and text[0].isupper():
        # Dosya/komut adı değilse ilk harfi büyüt (örn. jrc_web.py küçük kalabilir, düz cümle büyür)
        first_token = cleaned.split()[0]
        if "/" not in first_token and "_" not in first_token and "." not in first_token:
            cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def split_sentences_tr(text: str) -> list[str]:
    """Dosya adları (jrc_web.py), tarihler (19.11) ve kısaltmaları (örn., sat.) bölmeden cümlelere ayırır."""
    text = clean_markdown(text)
    if not text:
        return []
    sentences: list[str] = []
    start = 0
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in ".!?":
            # Üç nokta (...) vb. sonuna ilerle
            j = i
            while j + 1 < n and text[j + 1] in ".!?":
                j += 1
            # Sonrasında boşluk + büyük harf / rakam / parantez / tırnak gelmeli
            k = j + 1
            while k < n and text[k] == " ":
                k += 1
            if k > j + 1 and k < n and (
                text[k].isupper() or text[k] in "ÇĞİÖŞÜ\"“'(["
            ):
                # Noktadan önceki kelime kısaltma mı veya tek haneli sıra numarası mı?
                prev_chunk = text[start:i].rstrip()
                prev_word_m = re.search(r"([A-Za-zÇĞİÖŞÜçğıöşü0-9_]+)$", prev_chunk)
                prev_word = (prev_word_m.group(1) if prev_word_m else "").lower()
                if prev_word not in ABBREV_WORDS and not (len(prev_word) == 1 and prev_word.isdigit()):
                    seg = text[start : j + 1].strip()
                    if seg:
                        sentences.append(seg)
                    start = k
                    i = k
                    continue
        i += 1
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def summarize_1_2_sentences(text: str, max_chars: int = 360) -> str:
    """Metni en fazla 1-2 özlü Türkçe cümleye indirger."""
    cleaned = strip_report_disclaimer(clean_markdown(text))
    # Açık iş kuyruğu ayrı tutulacağı için ana özetten ayır
    cleaned = re.split(r"\s+(?:Açık iş|AÇIK İŞ|Doğrulanamayan açık iş)\s*:", cleaned)[0].strip()
    sents = split_sentences_tr(cleaned)
    if not sents:
        return ""
    chosen = sents[0]
    if len(sents) > 1 and len(chosen) < 210 and len(chosen) + 1 + len(sents[1]) <= max_chars:
        chosen = f"{chosen} {sents[1]}"
    if len(chosen) > max_chars:
        cut = chosen[: max_chars - 1].rsplit(" ", 1)[0]
        chosen = (cut or chosen[: max_chars - 1]).rstrip(",;:") + "…"
    if chosen and chosen[-1] not in ".!?…":
        chosen += "."
    return chosen


def extract_open_item(text: str) -> str:
    """Not içindeki 'Açık iş: ...' bölümünü tek cümle olarak ayıklar."""
    m = re.search(r"(?:Açık iş|AÇIK İŞ|Doğrulanamayan açık iş)\s*:\s*(.+)$", clean_markdown(text))
    if not m:
        return ""
    return summarize_1_2_sentences(m.group(1), max_chars=260)


def classify_project(text: str) -> str:
    """Metni anahtar kelime ağırlığına göre proje/site grubuna atar."""
    low = (text or "").lower()
    best_key = "diger"
    best_score = 0
    for key, _label, rules in PROJECT_GROUPS:
        score = 0
        for kw, weight in rules:
            if kw in low:
                score += weight
        if score > best_score:
            best_score = score
            best_key = key
    return best_key


def group_label(key: str) -> str:
    for k, label, _ in PROJECT_GROUPS:
        if k == key:
            return label
    return "Diğer Çalışmalar ve Genel İşlemler"


def make_short_title(task_text: str, note_text: str) -> tuple[str, str]:
    """(başlık, gövde_kaynağı) üretir; başlık ve özetin birebir tekrar etmemesini sağlar."""
    clean_note = strip_report_disclaimer(clean_markdown(note_text)) if note_text else ""
    clean_task = clean_markdown(task_text) if task_text else ""

    # 1) Not 'Kısa Başlık: Açıklama...' biçimindeyse doğrudan ayır (URL/port ve açık parantezleri bölme)
    if clean_note and ": " in clean_note:
        head, rest = clean_note.split(": ", 1)
        head, rest = head.strip(), rest.strip()
        if (
            6 <= len(head) <= 82
            and rest
            and head.count("(") == head.count(")")
            and head.count('"') % 2 == 0
            and not head.lower().startswith(("http", "rapora", "not", "dikkat", "uyarı", "ör", "değil"))
            and "://" not in head
        ):
            return head, rest

    # 2) Hem kullanıcı görevi hem Ortak Not varsa: görev başlık, Ortak Not (yapılan/sonuç) özet olur
    if clean_task and clean_note:
        # Çok uzun görev metninin ilk cümlesini/ifadesini başlık yap
        t_first = split_sentences_tr(clean_task)[0] if split_sentences_tr(clean_task) else clean_task
        t_first = t_first.strip("\"' ")
        if len(t_first) > 86:
            t_first = (t_first[:83].rsplit(" ", 1)[0] or t_first[:83]).rstrip(",;:") + "…"
        return t_first, clean_note

    # 3) Yalnız Ortak Not varsa: ilk cümleden kısa konu başlığı çıkar veya ilk cümleyi başlık/özet dengeli kur
    if clean_note:
        sents = split_sentences_tr(clean_note)
        if len(sents) >= 2 and len(sents[0]) <= 90:
            return sents[0].rstrip("."), " ".join(sents[1:])
        # Tek uzun cümle ise ilk 6-8 kelimeden konu etiketi üret
        words = clean_note.split()
        topic = " ".join(words[:7]).rstrip(",.;:")
        if len(words) > 7:
            topic += "…"
        return topic, clean_note

    # 4) Yalnız görev metni varsa
    if clean_task:
        t_first = split_sentences_tr(clean_task)[0] if split_sentences_tr(clean_task) else clean_task
        t_first = t_first.strip("\"' ")
        if len(t_first) > 86:
            t_first = (t_first[:83].rsplit(" ", 1)[0] or t_first[:83]).rstrip(",;:") + "…"
        return t_first, ""

    return "Kayıtlı işlem", ""


# ---------------------------------------------------------------------------
# Veri Toplama
# ---------------------------------------------------------------------------

def parse_context_md(target_date: dt.date) -> dict[str, Any]:
    """`.jrc_context.md` içinden hedef güne ait Son Görevler satırlarını ve Ortak Notlar maddelerini okur."""
    result: dict[str, Any] = {"tasks": [], "notes": [], "context_year": target_date.year}
    if not CONTEXT_MD.exists():
        return result
    try:
        raw = CONTEXT_MD.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    m_upd = re.search(r"Son Hafıza Güncellemesi\*\*:\s*(\d{4})-\d{2}-\d{2}", raw)
    ctx_year = int(m_upd.group(1)) if m_upd else dt.date.today().year
    result["context_year"] = ctx_year
    if target_date.year != ctx_year:
        return result

    ddmm = target_date.strftime("%d.%m")

    # 1. Son Görevler tablosu
    for line in raw.splitlines():
        s = line.strip()
        if not s.startswith("|") or ":---" in s or "Görev / Prompt" in s:
            continue
        cols = [c.strip() for c in s.strip("|").split("|")]
        if len(cols) < 5:
            continue
        no_col, ts_col, agent_col, effort_col, task_col = cols[:5]
        if not ts_col.startswith(ddmm):
            continue
        hm = ts_col[len(ddmm) :].strip() or "00:00"
        m_no = re.search(r"emir(\d+)", no_col, re.IGNORECASE)
        result["tasks"].append(
            {
                "no": int(m_no.group(1)) if m_no else None,
                "time": hm,
                "agent": clean_markdown(agent_col),
                "effort": clean_markdown(effort_col),
                "task": clean_markdown(task_col),
            }
        )

    # 2. Ortak Notlar
    notes_block = raw
    if USER_NOTES_START in raw and USER_NOTES_END in raw:
        notes_block = raw.split(USER_NOTES_START, 1)[1].split(USER_NOTES_END, 1)[0]

    note_pat = re.compile(rf"^-\s*\[{re.escape(ddmm)}(?:\s+(\d{{2}}:\d{{2}}))?\]\s*(.+)$")
    for line in notes_block.splitlines():
        m = note_pat.match(line.strip())
        if not m:
            continue
        hm = m.group(1) or ""
        body = m.group(2).strip()
        emir_refs = [int(x) for x in EMIR_RE.findall(body)]
        result["notes"].append(
            {
                "time": hm,
                "text": body,
                "emir_refs": emir_refs,
            }
        )
    return result


def parse_state_history(target_date: dt.date, ctx_year: int) -> list[dict[str, Any]]:
    """`.jrc_state.json` içindeki tam metinli task_history kayıtlarını okur."""
    if not STATE_JSON.exists() or target_date.year != ctx_year:
        return []
    try:
        data = json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ddmm = target_date.strftime("%d.%m")
    out: list[dict[str, Any]] = []
    for item in data.get("task_history") or []:
        if not isinstance(item, dict):
            continue
        ts = str(item.get("timestamp") or "").strip()
        if not ts.startswith(ddmm):
            continue
        hm = ts[len(ddmm) :].strip() or "00:00"
        out.append(
            {
                "no": item.get("no") if isinstance(item.get("no"), int) else None,
                "time": hm,
                "tool": str(item.get("tool") or ""),
                "model": str(item.get("model") or ""),
                "effort": str(item.get("effort") or ""),
                "task": str(item.get("task") or "").strip(),
            }
        )
    return out


def extract_job_log_info(log_path: Path) -> tuple[str, str]:
    """`web_jobs/<id>.log` içinden (varsa_director_notu, yürütücü_yanıt_özeti) döndürür."""
    if not log_path.exists():
        return "", ""
    try:
        raw = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""

    clean_full = ANSI_RE.sub("", raw)
    director_note = ""
    for m in NOTE_LOG_RE.finditer(clean_full):
        cand = m.group(1).strip()
        if cand and not cand.startswith("kalıcı değişiklik yok"):
            director_note = cand

    # Önce ANSI sarmallı (\x1b[2m...\x1b[0m) çok paragraflı yönetici iş emrini ham metinden çıkar
    raw_without_plan = RAW_DIRECTOR_BLOCK_RE.sub("", raw)
    body = ANSI_RE.sub("", raw_without_plan)
    m_cmd = ROUTER_CMD_RE.search(body)
    if m_cmd:
        body = body[m_cmd.end() :]
    body = DIRECTOR_FOOTER_RE.sub("", body)
    lines: list[str] = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("✓ Ajan oturumu tamamlandı", "⚡ Jev ", "├─", "└─")):
            continue
        if re.match(r"^[-*•]?\s*Rapor(?:\s+yazıldı)?\s*:", s):
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^[-*•]\s+", "", s)
        lines.append(s)

    joined = " ".join(lines[:8])
    return director_note, summarize_1_2_sentences(joined, max_chars=340)


def parse_web_jobs_and_reports(target_date: dt.date) -> list[dict[str, Any]]:
    """`~/.cache/jroute/web_jobs/*.json` (+ emir_seq.json, *.log, reports/*.md) kayıtlarını okur."""
    if not WEB_JOBS_DIR.exists():
        return []
    target_iso = target_date.isoformat()

    assigned: dict[str, int] = {}
    if EMIR_SEQ_JSON.exists():
        try:
            seq_obj = json.loads(EMIR_SEQ_JSON.read_text(encoding="utf-8"))
            if isinstance(seq_obj.get("assigned"), dict):
                assigned = {str(k): int(v) for k, v in seq_obj["assigned"].items() if isinstance(v, int)}
        except (OSError, ValueError):
            pass

    reports_by_pid: dict[int, Path] = {}
    if REPORTS_DIR.exists():
        for rp in REPORTS_DIR.glob("*.md"):
            m_pid = re.match(r"^\d{8}-\d{6}-(\d+)\.md$", rp.name)
            if m_pid:
                reports_by_pid[int(m_pid.group(1))] = rp

    jobs: list[dict[str, Any]] = []
    for jf in sorted(WEB_JOBS_DIR.glob("*.json")):
        try:
            meta = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        created = str(meta.get("created_at") or "")
        started = str(meta.get("started_at") or "")
        finished = str(meta.get("finished_at") or "")
        # Kanonik gün: görevin oluşturulduğu (yoksa başladığı) günün 00:00–23:59 aralığı
        canonical_ts = created or started or finished
        if not canonical_ts.startswith(target_iso):
            continue

        jid = str(meta.get("id") or jf.stem)
        no = meta.get("no") if isinstance(meta.get("no"), int) else assigned.get(jid)
        dec = meta.get("decision") if isinstance(meta.get("decision"), dict) else {}
        tool = str(dec.get("tool") or meta.get("tool") or "")
        model = str(dec.get("model") or "")
        agent_str = f"{tool}/{model}".strip("/")

        log_note, log_summary = extract_job_log_info(WEB_JOBS_DIR / f"{jf.stem}.log")
        if not log_summary and isinstance(meta.get("pid"), int) and meta["pid"] in reports_by_pid:
            try:
                rep_txt = reports_by_pid[meta["pid"]].read_text(encoding="utf-8", errors="replace")
                log_summary = summarize_1_2_sentences(rep_txt, max_chars=320)
            except OSError:
                pass

        # Görüntülenecek saat: o gün içindeki bitiş veya başlangıç/oluşturma saati
        disp_ts = finished if finished.startswith(target_iso) else (started if started.startswith(target_iso) else created)
        hm = disp_ts[11:16] if len(disp_ts) >= 16 else "00:00"

        jobs.append(
            {
                "id": jid,
                "no": no,
                "created_at": created,
                "started_at": started,
                "finished_at": finished,
                "time": hm,
                "status": str(meta.get("status") or "unknown"),
                "dry_run": bool(meta.get("dry_run")),
                "exit_code": meta.get("exit_code"),
                "agent": agent_str,
                "task": clean_markdown(str(meta.get("task") or "")),
                "log_note": log_note,
                "log_summary": log_summary,
            }
        )
    return jobs


def parse_gate_ledger(target_date: dt.date) -> dict[str, Any]:
    """`~/.cache/jroute/gate.jsonl` (ve ayrı inode ise odoo addons ledger) kayıtlarını okur."""
    target_iso = target_date.isoformat()
    seen_inodes: set[tuple[int, int]] = set()
    seen_lines: set[str] = set()
    entries: list[dict[str, Any]] = []

    for path in (GATE_JSONL, EXTRA_LEDGER_JSONL):
        if not path.exists():
            continue
        try:
            st = path.stat()
            inode_key = (st.st_dev, st.st_ino)
            if inode_key in seen_inodes:
                continue
            seen_inodes.add(inode_key)
            for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                s = raw_line.strip()
                if not s or target_iso not in s or s in seen_lines:
                    continue
                try:
                    rec = json.loads(s)
                except ValueError:
                    continue
                if str(rec.get("ts") or "")[:10] != target_iso:
                    continue
                seen_lines.add(s)
                entries.append(rec)
        except OSError:
            continue

    entries.sort(key=lambda r: str(r.get("ts") or ""))
    decisions = [r for r in entries if r.get("type") == "decision"]
    results = [r for r in entries if r.get("type") == "result"]
    fallbacks = [r for r in entries if r.get("type") == "fallback"]

    by_source: Counter[str] = Counter(str(r.get("source") or "bilinmeyen") for r in decisions)
    total_sec = sum(float(r.get("s") or 0.0) for r in results)

    return {
        "entries": entries,
        "decisions": decisions,
        "results": results,
        "fallbacks": fallbacks,
        "by_source": dict(by_source),
        "total_result_seconds": round(total_sec, 1),
    }


def count_day_artifacts(target_date: dt.date, exclude_pdf: Path) -> list[str]:
    """Hedef günde `artifacts/` altında üretilmiş/güncellenmiş dosyaları listeler."""
    if not ARTIFACTS_DIR.exists():
        return []
    out: list[str] = []
    for p in sorted(ARTIFACTS_DIR.iterdir()):
        if not p.is_file() or p.resolve() == exclude_pdf.resolve() or p.name.startswith("."):
            continue
        try:
            mdate = dt.datetime.fromtimestamp(p.stat().st_mtime).date()
        except OSError:
            continue
        if mdate == target_date:
            out.append(p.name)
    return out


# ---------------------------------------------------------------------------
# Birleştirme ve Tekilleştirme
# ---------------------------------------------------------------------------

def _hm_to_min(hm: str) -> int | None:
    if not hm or ":" not in hm:
        return None
    try:
        h, m = hm.split(":", 1)
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def build_work_items(
    target_date: dt.date,
    ctx_data: dict[str, Any],
    state_tasks: list[dict[str, Any]],
    web_jobs: list[dict[str, Any]],
    gate_data: dict[str, Any],
) -> tuple[list[WorkItem], list[str], dict[str, int]]:
    """Tüm kaynakları tekilleştirip proje bazlı maddelere ve istatistiklere dönüştürür."""
    notes = list(ctx_data["notes"])
    real_jobs = [j for j in web_jobs if not j.get("dry_run")]
    dry_jobs_count = sum(1 for j in web_jobs if j.get("dry_run"))

    used_note_idxs: set[int] = set()
    job_to_note: dict[str, int] = {}

    # 1. Eşleştirme: web_job log'undaki "✓ .jrc_context.md notu:" veya açık emirN referansı
    for job in real_jobs:
        jid = job["id"]
        jno = job.get("no")
        lnote = clean_markdown(job.get("log_note") or "")
        for idx, n in enumerate(notes):
            if idx in used_note_idxs:
                continue
            ntext_clean = clean_markdown(n["text"])
            if lnote and (lnote[:90] in ntext_clean or ntext_clean[:90] in lnote):
                job_to_note[jid] = idx
                used_note_idxs.add(idx)
                break
            if jno and jno in n.get("emir_refs", []):
                job_to_note[jid] = idx
                used_note_idxs.add(idx)
                break

    # 2. Eşleştirme: bitiş zamanı penceresi (örn. doğrudan Opus ile çalışan işler)
    target_iso = target_date.isoformat()
    for job in real_jobs:
        jid = job["id"]
        if jid in job_to_note:
            continue
        st_hm = job["started_at"][11:16] if job["started_at"].startswith(target_iso) else "00:00"
        fn_hm = job["finished_at"][11:16] if job["finished_at"].startswith(target_iso) else ""
        st_m = _hm_to_min(st_hm)
        fn_m = _hm_to_min(fn_hm) if fn_hm else None
        if st_m is None or fn_m is None:
            continue
        for idx, n in enumerate(notes):
            if idx in used_note_idxs:
                continue
            nm = _hm_to_min(n["time"])
            if nm is not None and (st_m - 1) <= nm <= (fn_m + 2):
                job_to_note[jid] = idx
                used_note_idxs.add(idx)
                break

    items: list[WorkItem] = []
    open_items: list[str] = []
    seen_task_fingerprints: set[str] = set()

    # A) Web Jobs (eşleşen Ortak Not ile zenginleştirilmiş veya kendi log özetiyle)
    for job in real_jobs:
        jid = job["id"]
        task_txt = job["task"]
        if task_txt:
            seen_task_fingerprints.add(task_txt[:48].lower())
        note_obj = notes[job_to_note[jid]] if jid in job_to_note else None
        note_txt = note_obj["text"] if note_obj else ""

        title, body_src = make_short_title(task_txt, note_txt)
        if note_txt:
            summary = summarize_1_2_sentences(body_src or note_txt)
            op = extract_open_item(note_txt)
            if op:
                open_items.append(op)
        elif job["status"] == "running":
            summary = f"Görev çalışmaya devam ediyor ({job['agent'] or 'JRC'})."
        elif job["status"] == "queued":
            summary = "Görev kuyrukta sırasını bekliyor."
        elif job["status"] == "cancelled":
            summary = "Görev çalıştırılmadan kuyruktan çıkarıldı (iptal edildi)."
        elif job.get("log_summary"):
            summary = job["log_summary"]
        else:
            durum_ek = "tamamlandı" if job.get("exit_code") in (0, None) else f"çıkış kodu {job.get('exit_code')} ile sonlandı"
            summary = f"JRC üzerinden ({job['agent'] or 'otomatik'}) yürütülerek {durum_ek}."

        combined_for_group = f"{task_txt} {note_txt}"
        t_disp = (note_obj["time"] if note_obj and note_obj["time"] else job["time"]) or "00:00"
        items.append(
            WorkItem(
                group_key=classify_project(combined_for_group),
                time_str=t_disp,
                sort_key=f"{t_disp}_{job.get('no') or 9999:04d}",
                title=title,
                summary=summary,
                emir_no=job.get("no"),
                agent=job.get("agent", ""),
            )
        )

    # B) Eşleşmemiş Ortak Notlar (terminal jrc/claude görevleri veya doğrudan eklenen mimari kararlar)
    for idx, n in enumerate(notes):
        if idx in used_note_idxs:
            continue
        ntext = n["text"]
        title, body_src = make_short_title("", ntext)
        summary = summarize_1_2_sentences(body_src or ntext)
        op = extract_open_item(ntext)
        if op:
            open_items.append(op)
        t_disp = n["time"] or "00:00"
        emir_no = n["emir_refs"][0] if n.get("emir_refs") else None
        items.append(
            WorkItem(
                group_key=classify_project(ntext),
                time_str=t_disp,
                sort_key=f"{t_disp}_{idx:04d}",
                title=title,
                summary=summary,
                emir_no=emir_no,
            )
        )

    # C) `.jrc_state.json` / `.jrc_context.md` Son Görevler veya `gate.jsonl` içindeki terminal jrc/jroute işleri
    all_cli_tasks: list[dict[str, Any]] = []
    for st in state_tasks:
        all_cli_tasks.append(
            {
                "no": st.get("no"),
                "time": st.get("time") or "00:00",
                "agent": f"{st.get('tool', '')}/{st.get('model', '')}".strip("/"),
                "task": clean_markdown(st.get("task") or ""),
            }
        )
    for ct in ctx_data["tasks"]:
        all_cli_tasks.append(
            {
                "no": ct.get("no"),
                "time": ct.get("time") or "00:00",
                "agent": ct.get("agent") or "",
                "task": clean_markdown(ct.get("task") or ""),
            }
        )
    for gd in gate_data["decisions"]:
        if gd.get("source") in ("jrc", "jroute"):
            all_cli_tasks.append(
                {
                    "no": None,
                    "time": str(gd.get("ts") or "")[11:16] or "00:00",
                    "agent": f"{gd.get('tool', '')}/{gd.get('model', '')}".strip("/"),
                    "task": clean_markdown(str(gd.get("task") or "")),
                }
            )

    known_emir_nos = {it.emir_no for it in items if it.emir_no is not None}
    for ct in all_cli_tasks:
        t_txt = ct["task"].rstrip("…").strip()
        if not t_txt:
            continue
        if ct["no"] and ct["no"] in known_emir_nos:
            continue
        fp = t_txt[:42].lower()
        if any(fp in k or k in fp for k in seen_task_fingerprints if k):
            continue
        # Eğer aynı dakikada veya hemen sonrasında bir Ortak Not varsa mükerrer yazma
        ct_min = _hm_to_min(ct["time"])
        matched_existing_note = False
        if ct_min is not None:
            for n in notes:
                nm = _hm_to_min(n["time"])
                if nm is not None and 0 <= (nm - ct_min) <= 20:
                    # Kelime kesişimi kontrolü
                    w_task = {w for w in re.findall(r"\w{4,}", t_txt.lower())}
                    w_note = {w for w in re.findall(r"\w{4,}", n["text"].lower())}
                    if w_task & w_note:
                        matched_existing_note = True
                        break
        if matched_existing_note:
            seen_task_fingerprints.add(fp)
            continue

        seen_task_fingerprints.add(fp)
        if ct["no"]:
            known_emir_nos.add(ct["no"])
        title, _ = make_short_title(ct["task"], "")
        items.append(
            WorkItem(
                group_key=classify_project(ct["task"]),
                time_str=ct["time"],
                sort_key=f"{ct['time']}_cli",
                title=title,
                summary=f"Terminal/JRC üzerinden ({ct['agent'] or 'otomatik'}) görev çalıştırıldı.",
                emir_no=ct["no"],
                agent=ct["agent"],
            )
        )

    # D) `gate.jsonl` üzerindeki somut servis/ajan işlemleri (Telegram satış/ajan, vitrin, blog yazarı, gece raporu)
    action_sources: dict[str, tuple[str, str]] = {
        "eretna_ajan": ("odoo", "Eretna Telegram Ajanı İşlemleri"),
        "eretna_satis": ("odoo", "Eretna Telegram Satış Temsilcisi İşlemleri"),
        "urun_aciklama": ("odoo", "Eretna Ürün Açıklaması Üretimleri"),
        "showcase": ("odoo", "Yönetim Ürün Kartı Vitrin (Showcase) Görselleri"),
        "foto_duzenle": ("odoo", "Yönetim Ürün Kartı AI Fotoğraf Düzenlemeleri"),
        "keabilgisayar_yazar": ("webler", "Web Siteleri Günlük Blog Yazarı"),
        "yonetici_gece": ("jrc", "Gece Yönetici Araştırma ve Denetim Görevi"),
        "finansci": ("odoo", "Masraf ve Finans Ayrıştırma İşlemleri"),
    }
    for src_key, (grp_key, label_title) in action_sources.items():
        src_decs = [d for d in gate_data["decisions"] if d.get("source") == src_key]
        if not src_decs:
            continue
        first_hm = str(src_decs[0].get("ts") or "")[11:16] or "00:00"
        last_hm = str(src_decs[-1].get("ts") or "")[11:16] or first_hm
        t_range = first_hm if first_hm == last_hm else f"{first_hm}–{last_hm}"
        samples: list[str] = []
        for d in src_decs:
            stask = clean_markdown(str(d.get("task") or ""))
            stask = re.sub(r"\[jrc:[^\]]+\]\s*", "", stask).strip()
            if stask and stask not in samples:
                samples.append(stask[:70] + ("…" if len(stask) > 70 else ""))
            if len(samples) >= 2:
                break
        sample_str = f" Örnek: {'; '.join(samples)}." if samples else ""
        summary = summarize_1_2_sentences(
            f"{src_key} geçidi üzerinden toplam {len(src_decs)} işlem yürütüldü.{sample_str}"
        )
        items.append(
            WorkItem(
                group_key=grp_key,
                time_str=t_range,
                sort_key=f"{first_hm}_{src_key}",
                title=label_title,
                summary=summary,
            )
        )

    items.sort(key=lambda x: x.sort_key)

    # Açık işleri tekilleştir
    dedup_open: list[str] = []
    seen_open: set[str] = set()
    for op in open_items:
        k = op[:60].lower()
        if k not in seen_open:
            seen_open.add(k)
            dedup_open.append(op)

    by_src = gate_data["by_source"]
    jrc_emir_count = max(
        len(real_jobs),
        by_src.get("jrc", 0) + by_src.get("jroute", 0),
    )
    director_count = by_src.get("director", 0)
    bot_gate_count = sum(
        cnt for src, cnt in by_src.items() if src not in ("jrc", "jroute", "director")
    )
    stats = {
        "total_items": len(items),
        "notes_count": len(notes),
        "jrc_emir_count": jrc_emir_count,
        "dry_run_count": dry_jobs_count,
        "director_count": director_count,
        "bot_gate_count": bot_gate_count,
        "proxy_count": by_src.get("proxy", 0),
        "hermes_count": by_src.get("hermes", 0),
        "peribot_count": by_src.get("peribot", 0),
        "gate_total_decisions": len(gate_data["decisions"]),
    }
    return items, dedup_open, stats


# ---------------------------------------------------------------------------
# PDF Üretimi (fpdf2 + DejaVu Sans, tam Türkçe karakter desteği)
# ---------------------------------------------------------------------------

class GunlukOzetPDF(FPDF):
    def __init__(self, date_label: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.date_label = date_label
        self.set_margins(left=14, top=15, right=14)
        self.set_auto_page_break(auto=True, margin=16)

        self.add_font("DejaVu", "", str(FONT_DIR / "DejaVuSans.ttf"))
        self.add_font("DejaVu", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
        self.add_font("DejaVu", "I", str(FONT_DIR / "DejaVuSans-Oblique.ttf"))
        self.add_font("DejaVuMono", "", str(FONT_DIR / "DejaVuSansMono.ttf"))
        self.alias_nb_pages()

        # DejaVu font yüzlerinin tümünde bulunan ortak karakter kümesi (eksik glif / emoji kutusu önlemi)
        c_reg = set(self.fonts["dejavu"].cmap.keys())
        c_bold = set(self.fonts["dejavuB"].cmap.keys())
        c_ital = set(self.fonts["dejavuI"].cmap.keys())
        self._allowed_codepoints = c_reg & c_bold & c_ital

    def safe(self, text: str) -> str:
        """DejaVu TTF içinde olmayan emoji/özel glifleri temizler; Türkçe harfleri (%100) korur."""
        if not text:
            return ""
        replacements = {
            "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
            "\u2192": "->", "\u2190": "<-", "\u2194": "<->",
        }
        out_chars: list[str] = []
        for ch in text:
            if ch in ("\n", "\t", " "):
                out_chars.append(ch)
                continue
            if ch in replacements and ord(ch) not in self._allowed_codepoints:
                out_chars.append(replacements[ch])
                continue
            if ord(ch) in self._allowed_codepoints:
                out_chars.append(ch)
        return re.sub(r" {2,}", " ", "".join(out_chars)).strip()

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("DejaVu", "I", 8)
        self.set_text_color(105, 115, 128)
        self.cell(
            0,
            6,
            self.safe(f"Günlük Çalışma Özeti  |  Tarih: {self.date_label} (00:00 – 23:59)"),
            align="L",
        )
        self.ln(6.5)
        self.set_draw_color(218, 224, 232)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_draw_color(218, 224, 232)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)
        self.set_font("DejaVu", "", 8)
        self.set_text_color(105, 115, 128)
        self.cell(
            0,
            5,
            self.safe(f"Peribot Günlük Döküm · {self.date_label}  |  Sayfa {self.page_no()} / {{nb}}"),
            align="C",
        )

    def kapak(self, total_items: int) -> None:
        self.set_fill_color(19, 41, 75)
        self.rect(14, 14, 182, 28, style="F")
        self.set_xy(19, 17.5)
        self.set_font("DejaVu", "B", 15)
        self.set_text_color(255, 255, 255)
        self.cell(
            172,
            8,
            self.safe("GÜNLÜK ÇALIŞMA VE İŞLEM ÖZETİ RAPORU"),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        self.set_x(19)
        self.set_font("DejaVu", "", 9.2)
        self.set_text_color(205, 220, 242)
        self.cell(
            172,
            5.5,
            self.safe(
                f"Tarih: {self.date_label} (00:00 – 23:59)  |  Toplam İş: {total_items}  |  Kanal: Peribot (@teleperiBot)"
            ),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        self.set_x(19)
        self.set_font("DejaVu", "I", 8.2)
        self.set_text_color(178, 198, 226)
        self.cell(
            172,
            5,
            self.safe("Kaynaklar: .jrc_context.md, ~/.cache/jroute/gate.jsonl, web_jobs/ ve JRC emir geçmişi"),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        self.set_y(46)

    def bilgi_kutusu(self, baslik: str, metin: str) -> None:
        x = self.l_margin
        y = self.get_y()
        self.set_font("DejaVu", "B", 9)
        h_title = self.multi_cell(174, 4.6, self.safe(baslik), dry_run=True, output="HEIGHT")
        self.set_font("DejaVu", "", 8.4)
        h_body = self.multi_cell(174, 4.4, self.safe(metin), dry_run=True, output="HEIGHT")
        box_h = h_title + h_body + 6.5

        self.set_fill_color(243, 247, 252)
        self.set_draw_color(196, 212, 234)
        self.rect(x, y, 182, box_h, style="DF")

        self.set_xy(x + 4, y + 3)
        self.set_font("DejaVu", "B", 9)
        self.set_text_color(19, 41, 75)
        self.multi_cell(174, 4.6, self.safe(baslik), new_x="LMARGIN", new_y="NEXT")
        self.set_x(x + 4)
        self.set_font("DejaVu", "", 8.4)
        self.set_text_color(45, 55, 72)
        self.multi_cell(174, 4.4, self.safe(metin), new_x="LMARGIN", new_y="NEXT")
        self.set_y(y + box_h + 3)

    def kpi_satiri(self, kutular: list[tuple[str, str, str]]) -> None:
        w_total = 182.0
        gap = 3.0
        n = len(kutular)
        box_w = (w_total - gap * (n - 1)) / n
        box_h = 17.0
        y = self.get_y()
        for i, (etiket, deger, alt) in enumerate(kutular):
            bx = self.l_margin + i * (box_w + gap)
            self.set_fill_color(246, 249, 253)
            self.set_draw_color(210, 220, 235)
            self.rect(bx, y, box_w, box_h, style="DF")
            self.set_xy(bx + 2.5, y + 2)
            self.set_font("DejaVu", "B", 11)
            self.set_text_color(29, 78, 216)
            self.cell(box_w - 5, 5.2, self.safe(deger))
            self.set_xy(bx + 2.5, y + 7.5)
            self.set_font("DejaVu", "B", 7.6)
            self.set_text_color(30, 41, 59)
            self.cell(box_w - 5, 4, self.safe(etiket))
            self.set_xy(bx + 2.5, y + 11.7)
            self.set_font("DejaVu", "", 6.9)
            self.set_text_color(100, 116, 139)
            self.cell(box_w - 5, 3.8, self.safe(alt))
        self.set_y(y + box_h + 3.5)

    def bolum(self, baslik: str) -> None:
        if self.get_y() > 232:
            self.add_page()
        self.ln(2)
        y = self.get_y()
        self.set_fill_color(235, 242, 252)
        self.rect(self.l_margin, y, 182, 8, style="F")
        self.set_draw_color(37, 99, 235)
        self.set_line_width(0.9)
        self.line(self.l_margin, y, self.l_margin, y + 8)
        self.set_line_width(0.2)
        self.set_xy(self.l_margin + 3.5, y + 1.4)
        self.set_font("DejaVu", "B", 10.2)
        self.set_text_color(19, 41, 75)
        self.cell(175, 5.2, self.safe(baslik), new_x="LMARGIN", new_y="NEXT")
        self.ln(2.5)

    def madde_karti(self, item: WorkItem) -> None:
        if self.get_y() > 252:
            self.add_page()
        meta_parts: list[str] = []
        if item.time_str and item.time_str != "00:00":
            meta_parts.append(item.time_str)
        if item.emir_no is not None:
            meta_parts.append(f"emir{item.emir_no}")
        meta_badge = f"  [{' · '.join(meta_parts)}]" if meta_parts else ""

        self.set_x(self.l_margin)
        self.set_font("DejaVu", "B", 9.0)
        self.set_text_color(19, 41, 75)
        self.multi_cell(
            182,
            4.6,
            self.safe(f"• {item.title}{meta_badge}"),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        if item.summary:
            if self.get_y() > 268:
                self.add_page()
            self.set_x(self.l_margin + 4)
            self.set_font("DejaVu", "", 8.3)
            self.set_text_color(42, 50, 62)
            self.multi_cell(
                178,
                4.4,
                self.safe(item.summary),
                new_x="LMARGIN",
                new_y="NEXT",
            )
        self.ln(1.6)

    def bos_gun_kutusu(self) -> None:
        self.ln(4)
        x = self.l_margin
        y = self.get_y()
        self.set_fill_color(248, 250, 252)
        self.set_draw_color(203, 213, 225)
        self.rect(x, y, 182, 28, style="DF")
        self.set_xy(x, y + 6.5)
        self.set_font("DejaVu", "B", 11.5)
        self.set_text_color(30, 41, 59)
        self.cell(182, 6, self.safe("Bugün kayıtlı iş yok."), align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_x(x + 8)
        self.set_font("DejaVu", "", 8.6)
        self.set_text_color(100, 116, 139)
        self.multi_cell(
            166,
            4.6,
            self.safe(
                f"{self.date_label} (00:00 – 23:59) aralığında .jrc_context.md, JRC emir geçmişi ve geçit "
                "kayıtlarında tamamlanmış kullanıcı işi bulunmamaktadır."
            ),
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        self.set_y(y + 32)


def generate_pdf(target_date: dt.date) -> tuple[Path, int, dict[str, int]]:
    """Hedef gün için PDF özetini üretir ve (pdf_yolu, iş_sayısı, istatistikler) döndürür."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out_pdf = ARTIFACTS_DIR / f"gunluk_ozet_{target_date.strftime('%Y%m%d')}.pdf"
    date_label = target_date.strftime("%d.%m.%Y")

    ctx_data = parse_context_md(target_date)
    state_tasks = parse_state_history(target_date, ctx_data["context_year"])
    web_jobs = parse_web_jobs_and_reports(target_date)
    gate_data = parse_gate_ledger(target_date)
    day_artifacts = count_day_artifacts(target_date, exclude_pdf=out_pdf)

    items, open_items, stats = build_work_items(
        target_date=target_date,
        ctx_data=ctx_data,
        state_tasks=state_tasks,
        web_jobs=web_jobs,
        gate_data=gate_data,
    )

    # Proje/site gruplarına ayır (tanımlı sıra + sonda diger)
    group_order = [k for k, _, _ in PROJECT_GROUPS] + ["diger"]
    grouped: dict[str, list[WorkItem]] = {k: [] for k in group_order}
    for it in items:
        grouped.setdefault(it.group_key, []).append(it)
    active_groups = [(k, grouped[k]) for k in group_order if grouped.get(k)]

    pdf = GunlukOzetPDF(date_label=date_label)
    pdf.add_page()
    pdf.kapak(total_items=len(items))

    gen_stamp = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    art_note = f", {len(day_artifacts)} yeni artifact çıktısı" if day_artifacts else ""
    pdf.bilgi_kutusu(
        "Kapsam ve Doğrulanan Veri Kaynakları Özeti",
        f"Bu rapor {date_label} günü 00:00 – 23:59 aralığını kapsar (üretim zamanı: {gen_stamp}). "
        f"Veriler .jrc_context.md ({stats['notes_count']} ortak not), JRC emir geçmişi ({stats['jrc_emir_count']} görev"
        f"{', ' + str(stats['dry_run_count']) + ' dry-run hariç' if stats['dry_run_count'] else ''}) ve "
        f"gate.jsonl ({stats['gate_total_decisions']} geçit kararı{art_note}) taranarak derlenmiştir.",
    )

    pdf.kpi_satiri(
        [
            ("Toplam Kayıtlı İş", f"{len(items)} İş", f"{len(active_groups)} proje/site grubu"),
            ("JRC Görev & Emir", f"{stats['jrc_emir_count']} Emir", f"{stats['notes_count']} kalıcı ortak not"),
            ("Yönetici (Director)", f"{stats['director_count']} Çağrı", "İş emri ve denetim"),
            (
                "Bot & Servis Geçidi",
                f"{stats['bot_gate_count']} Karar",
                f"Proxy {stats['proxy_count']} · Hermes {stats['hermes_count']}",
            ),
        ]
    )

    if not items:
        pdf.bos_gun_kutusu()
    else:
        for sec_idx, (gkey, gitems) in enumerate(active_groups, 1):
            pdf.bolum(f"{sec_idx}. {group_label(gkey)} ({len(gitems)} iş)")
            for it in gitems:
                pdf.madde_karti(it)

        if open_items:
            pdf.bolum(f"{len(active_groups) + 1}. Açık Kalan İşler ve Takip Notları")
            for op in open_items:
                if pdf.get_y() > 266:
                    pdf.add_page()
                pdf.set_x(pdf.l_margin + 2)
                pdf.set_font("DejaVu", "", 8.2)
                pdf.set_text_color(124, 45, 18)
                pdf.multi_cell(180, 4.3, pdf.safe(f"– {op}"), new_x="LMARGIN", new_y="NEXT")
                pdf.ln(0.8)

    pdf.output(str(out_pdf))
    return out_pdf, len(items), stats


def send_via_peribot(pdf_path: Path, target_date: dt.date, item_count: int) -> dict[str, Any]:
    """`projeler/odoo/telegram_yanit.py` içindeki `belge_gonder` ile Peribot üzerinden gönderir."""
    if str(ODOO_DIR) not in sys.path:
        sys.path.insert(0, str(ODOO_DIR))
    from telegram_yanit import belge_gonder, cevap_kaydet  # type: ignore[import-not-found]

    date_label = target_date.strftime("%d.%m.%Y")
    if item_count > 0:
        caption = f"📋 Günlük Çalışma Özeti — {date_label} ({item_count} iş)"
    else:
        caption = f"📋 Günlük Çalışma Özeti — {date_label} (bugün kayıtlı iş yok)"

    resp = belge_gonder(TARGET_CHAT_ID, str(pdf_path), caption, bot=TARGET_BOT)
    cevap_kaydet(TARGET_BOT, TARGET_USER, caption, str(pdf_path))
    return resp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Günlük çalışma özeti PDF üretici ve Peribot Telegram göndericisi."
    )
    parser.add_argument(
        "--tarih",
        default=None,
        metavar="YYYY-MM-DD",
        help="Özetlenecek gün (YYYY-MM-DD, varsayılan: bugün).",
    )
    parser.add_argument(
        "--gonderme",
        action="store_true",
        help="Yalnızca PDF üret, Telegram'a gönderme.",
    )
    args = parser.parse_args(argv)

    if args.tarih:
        try:
            target_date = dt.date.fromisoformat(args.tarih)
        except ValueError:
            log_line(f"HATA: Geçersiz --tarih biçimi ({args.tarih!r}); YYYY-MM-DD bekleniyor.")
            return 2
    else:
        target_date = dt.datetime.now().date()

    try:
        pdf_path, item_count, stats = generate_pdf(target_date)
        size_b = pdf_path.stat().st_size
        log_line(
            f"PDF üretildi: {pdf_path} ({size_b} bayt, tarih={target_date.isoformat()}, "
            f"iş={item_count}, emir={stats['jrc_emir_count']}, not={stats['notes_count']}, "
            f"gate={stats['gate_total_decisions']})"
        )
    except Exception as exc:
        log_line(f"HATA: PDF üretimi başarısız ({target_date.isoformat()}): {exc}")
        return 1

    if args.gonderme:
        log_line("Gönderim atlandı (--gonderme seçildi).")
        return 0

    try:
        resp = send_via_peribot(pdf_path, target_date, item_count)
        ok = bool(resp.get("ok"))
        msg_id = (resp.get("result") or {}).get("message_id")
        bot_uname = ((resp.get("result") or {}).get("from") or {}).get("username", TARGET_BOT)
        log_line(
            f"Telegram gönderimi tamamlandı: ok={ok}, message_id={msg_id}, "
            f"bot=@{bot_uname}, chat_id={TARGET_CHAT_ID}, dosya={pdf_path.name}"
        )
        return 0 if ok else 1
    except Exception as exc:
        log_line(f"HATA: Telegram gönderimi başarısız: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
