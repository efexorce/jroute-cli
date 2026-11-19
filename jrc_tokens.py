#!/usr/bin/env python3
"""jrc token defteri — motor ve model bazında kalıcı token yakım kaydı.

Kaynaklar:
  • Claude Code oturum kayıtları (~/.claude/projects/**/*.jsonl): her API cevabının gerçek `usage` alanı.
    Claude bu dosyaları ~30 günde siler; bu yüzden her mesaj kalıcı SQLite defterine (tokens.db) işlenir.
  • Maliyet defteri (odoo/claude_maliyet_defteri.csv): agy (Antigravity) ve doğrudan API (OpenRouter/Google)
    çağrıları; ayrıca transcript'i silinmiş eski Claude çağrıları (tek seferlik geri doldurma).

Çıktı: jrc web konsolunun okuduğu özet JSON (odoo/addons/.jrc_tokens.json → jrc web konsolu 127.0.0.1:8787/#tokens · Token Harcama sekmesi).

Kullanım:
  jrc tokens            topla + özet yaz + terminalde göster
  jrc tokens --sessiz   yalnız topla ve yaz (cron)
  jrc tokens --json     özet JSON'u bas
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HOME = Path.home()
CACHE_DIR = HOME / ".cache" / "jroute"
DB_FILE = Path(os.environ.get("JRC_TOKENS_DB") or CACHE_DIR / "tokens.db")
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
PROJELER = HOME / "Desktop" / "jev" / "projeler"
CSV_DEFTER = Path(os.environ.get("JRC_MALIYET_CSV") or PROJELER / "odoo" / "claude_maliyet_defteri.csv")
AGY_QUOTA_CACHE = CACHE_DIR / "agy_quota_cache.json"
CLAUDE_QUOTA_CACHE = CACHE_DIR / "claude_quota_cache.json"
# Claude /usage alanları → panel etiketi (null gelenler atlanır)
CLAUDE_QUOTA_KEYS = [
    ("five_hour", "5 saatlik"),
    ("seven_day", "Haftalık"),
    ("seven_day_opus", "Haftalık · Opus"),
    ("seven_day_sonnet", "Haftalık · Sonnet"),
]


def _summary_file() -> Path:
    if os.environ.get("JRC_TOKENS_JSON"):
        return Path(os.environ["JRC_TOKENS_JSON"])
    addons = PROJELER / "odoo" / "addons"
    return (addons if addons.is_dir() else CACHE_DIR) / ".jrc_tokens.json"


# API liste fiyatı, $/1M token: (girdi, çıktı, önbellek okuma). Önbellek yazma = girdi × 1,25 (5 dk) / × 2 (1 saat).
# Claude aboneliğinde bu para ödenmez; "API eşdeğeri" yakımı karşılaştırmak içindir. Sıra önemli: ilk eşleşen önek.
PRICES: list[tuple[str, tuple[float, float, float]]] = [
    ("claude-fable-5-2", (8.0, 40.0, 0.20)),
    ("claude-mythos-5-2", (8.0, 40.0, 0.20)),
    ("claude-fable", (10.0, 50.0, 1.0)),
    ("claude-opus-5-5", (4.0, 20.0, 0.20)),
    ("claude-opus", (5.0, 25.0, 0.50)),
    ("claude-sonnet-5-5", (2.0, 10.0, 0.10)),
    ("claude-sonnet-5", (2.0, 10.0, 0.20)),
    ("claude-sonnet", (3.0, 15.0, 0.30)),
    ("claude-haiku-5-5", (0.10, 0.50, 0.01)),
    ("claude-haiku", (1.0, 5.0, 0.10)),
]


def price_for(model: str) -> tuple[float, float, float] | None:
    for prefix, p in PRICES:
        if model.startswith(prefix):
            return p
    return None


def cost_usd(model: str, inp: int, out: int, cr: int, cw5: int, cw1: int) -> float:
    p = price_for(model)
    if not p:
        return 0.0
    return (inp * p[0] + out * p[1] + cr * p[2] + cw5 * p[0] * 1.25 + cw1 * p[0] * 2.0) / 1e6


# --- kalıcı defter ---------------------------------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_FILE), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS msg (
            id TEXT PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, engine TEXT NOT NULL, model TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT '', session TEXT NOT NULL DEFAULT '',
            inp INTEGER NOT NULL DEFAULT 0, out INTEGER NOT NULL DEFAULT 0, cr INTEGER NOT NULL DEFAULT 0,
            cw5 INTEGER NOT NULL DEFAULT 0, cw1 INTEGER NOT NULL DEFAULT 0, usd REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS msg_ts ON msg(ts);
        CREATE INDEX IF NOT EXISTS msg_session ON msg(session);
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, offset INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sess (session TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """
    )
    return con


UPSERT = (
    "INSERT INTO msg (id, ts, day, engine, model, project, session, inp, out, cr, cw5, cw1, usd) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
    # Akış sırasında aynı mesaj birkaç satıra yazılır; çıktı sayacı en son (en büyük) değerdir.
    "ON CONFLICT(id) DO UPDATE SET out=excluded.out, usd=excluded.usd WHERE excluded.out > msg.out"
)


def _project_name(cwd: str, fallback: str) -> str:
    if cwd:
        p = Path(cwd)
        try:
            rel = p.relative_to(PROJELER)
            return "/".join(rel.parts[:2]) or "projeler"
        except ValueError:
            pass
        try:
            rel = p.relative_to(HOME)
            return "~/" + "/".join(rel.parts[:2]) if rel.parts else "~"
        except ValueError:
            return "/".join(p.parts[:2]) or cwd
    return fallback


def _user_text(msg: Any) -> str:
    """Oturumun ilk gerçek kullanıcı metni (sistem/araç blokları hariç) — oturum başlığı olarak."""
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    texts = [c] if isinstance(c, str) else [
        b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text"
    ]
    for raw in texts:
        s = str(raw)
        m = re.search(r"<gorev>\s*(.*?)\s*</gorev>", s, re.S)
        if m:
            inner = " ".join(m.group(1).split())
            if inner:
                return ("🧭 " + inner)[:140]
        t = " ".join(s.split())
        if t and not t.startswith("<") and not t.startswith("Caveat:"):
            return t[:140]
    return ""


def _fix_director_titles(con: sqlite3.Connection) -> None:
    """Eski yönetici oturumlarının kalıp başlıklarını bir kez silip jsonl'den gerçek <gorev> başlığıyla yeniler."""
    if con.execute("SELECT v FROM meta WHERE k='baslik_fix_v1'").fetchone():
        return
    bad = {
        r[0]
        for r in con.execute("SELECT session FROM sess WHERE title LIKE 'Sen JEV iş akışının yöneticisisin%'")
    }
    titles: list[tuple[str, str]] = []
    if bad and CLAUDE_PROJECTS.is_dir():
        for path in CLAUDE_PROJECTS.rglob("*.jsonl"):
            if not bad:
                break
            try:
                with path.open("rb") as fh:
                    for raw in fh:
                        if b'"usage"' in raw or b'"type":"user"' not in raw:
                            continue
                        try:
                            obj = json.loads(raw)
                        except Exception:
                            continue
                        if obj.get("type") != "user" or obj.get("isSidechain") or obj.get("isMeta"):
                            continue
                        session = str(obj.get("sessionId") or obj.get("session_id") or path.stem)
                        if session not in bad:
                            continue
                        t = _user_text(obj.get("message"))
                        if t:
                            titles.append((session, t))
                            bad.discard(session)
            except OSError:
                continue
    with con:
        con.execute("DELETE FROM sess WHERE title LIKE 'Sen JEV iş akışının yöneticisisin%'")
        if titles:
            con.executemany("INSERT OR IGNORE INTO sess (session, title) VALUES (?,?)", titles)
        con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('baslik_fix_v1', '1')")


def _scan_claude(con: sqlite3.Connection) -> int:
    if not CLAUDE_PROJECTS.is_dir():
        return 0
    _fix_director_titles(con)
    offsets = dict(con.execute("SELECT path, offset FROM files"))
    titled = {r[0] for r in con.execute("SELECT session FROM sess")}
    added = 0
    for path in CLAUDE_PROJECTS.rglob("*.jsonl"):
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        start = offsets.get(key, 0)
        if size == start:
            continue
        if size < start:
            start = 0
        rows: list[tuple] = []
        titles: list[tuple[str, str]] = []
        fallback = path.relative_to(CLAUDE_PROJECTS).parts[0]
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                data = fh.read()
        except OSError:
            continue
        end = data.rfind(b"\n")
        if end < 0:
            continue
        for raw in data[: end + 1].splitlines():
            is_usage = b'"usage"' in raw
            is_user = not is_usage and b'"type":"user"' in raw
            if not (is_usage or is_user):
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            session = str(obj.get("sessionId") or obj.get("session_id") or path.stem)
            msg = obj.get("message")
            if obj.get("type") == "user":
                if session not in titled and not obj.get("isSidechain") and not obj.get("isMeta"):
                    t = _user_text(msg)
                    if t:
                        titled.add(session)
                        titles.append((session, t))
                continue
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            model = str(msg.get("model") or "")
            if not model or model.startswith("<"):
                continue
            mid = msg.get("id") or obj.get("requestId") or obj.get("uuid")
            if not mid:
                continue
            try:
                ts = datetime.fromisoformat(str(obj.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            u = msg["usage"]
            inp = int(u.get("input_tokens") or 0)
            out = int(u.get("output_tokens") or 0)
            cr = int(u.get("cache_read_input_tokens") or 0)
            cw = int(u.get("cache_creation_input_tokens") or 0)
            cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
            cw1 = min(cw, int(cc.get("ephemeral_1h_input_tokens") or 0))
            cw5 = cw - cw1
            rows.append((
                str(mid), ts, datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), "claude", model,
                _project_name(str(obj.get("cwd") or ""), fallback), session,
                inp, out, cr, cw5, cw1, cost_usd(model, inp, out, cr, cw5, cw1),
            ))
        with con:
            if rows:
                con.executemany(UPSERT, rows)
            if titles:
                con.executemany("INSERT OR IGNORE INTO sess (session, title) VALUES (?,?)", titles)
            con.execute("INSERT OR REPLACE INTO files (path, offset) VALUES (?,?)", (key, start + end + 1))
        added += len(rows)
    return added


def _csv_engine(model: str) -> str:
    m = model.lower()
    if m.startswith("claude-"):
        return "claude"
    if "/" in m:
        return "api"  # OpenRouter (anthropic/…, google/…)
    if m.startswith("gemini") and m.rsplit("-", 1)[-1] in ("low", "medium", "high"):
        return "agy"
    return "api"


def _scan_csv(con: sqlite3.Connection) -> int:
    """agy + doğrudan API çağrıları maliyet defterinden gelir. Claude Code satırları transcript'lerde zaten vardır;
    yalnız transcript'i silinmiş eski günler (ilk transcript gününden öncesi) buradan geri doldurulur."""
    if not CSV_DEFTER.is_file():
        return 0
    row = con.execute("SELECT v FROM meta WHERE k='claude_ilk_gun'").fetchone()
    if row:
        first_day = row[0]
    else:
        first_day = (con.execute("SELECT MIN(day) FROM msg WHERE engine='claude'").fetchone()[0] or "9999")
        con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('claude_ilk_gun', ?)", (first_day,))
    rows: list[tuple] = []
    seen: dict[str, int] = {}
    try:
        with CSV_DEFTER.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    zaman = r["zaman"]
                    ts = datetime.strptime(zaman, "%Y-%m-%d %H:%M:%S").timestamp()
                    model = (r.get("model") or "").split(",")[0].strip()
                    inp, out = int(r.get("girdi_token") or 0), int(r.get("cikti_token") or 0)
                    cr, cw = int(r.get("onbellek_okuma_token") or 0), int(r.get("onbellek_yazma_token") or 0)
                    usd = float(r.get("maliyet_usd") or 0)
                except Exception:
                    continue
                if not model or not (inp or out or cr or cw or usd):
                    continue
                engine = _csv_engine(model)
                if engine == "claude" and zaman[:10] >= first_day:
                    continue
                h = hashlib.md5("|".join(str(r.get(k, "")) for k in sorted(r)).encode()).hexdigest()
                seen[h] = seen.get(h, 0) + 1
                rows.append((
                    f"csv:{h}:{seen[h]}", ts, zaman[:10], engine, model, r.get("kaynak") or "", "",
                    inp, out, cr, cw, 0, usd,
                ))
    except OSError:
        return 0
    with con:
        before = con.total_changes
        con.executemany(UPSERT.split(" ON CONFLICT")[0].replace("INSERT INTO", "INSERT OR IGNORE INTO"), rows)
        return con.total_changes - before


def collect() -> dict[str, int]:
    con = _db()
    try:
        n_claude = _scan_claude(con)
        n_csv = _scan_csv(con)
    finally:
        con.close()
    return {"claude": n_claude, "csv": n_csv}


# --- özet ------------------------------------------------------------------------------------------------------------

SUMS = "COUNT(*), SUM(inp), SUM(out), SUM(cr), SUM(cw5+cw1), SUM(usd)"


def _pack(r: tuple) -> dict[str, Any]:
    n, inp, out, cr, cw, usd = (x or 0 for x in r)
    return {"cagri": n, "girdi": inp, "cikti": out, "onbellek_okuma": cr, "onbellek_yazma": cw,
            "toplam": inp + out + cr + cw, "usd": round(usd, 4)}


def _window(con: sqlite3.Connection, since: float) -> dict[str, Any]:
    w = (since,)
    total = _pack(con.execute(f"SELECT {SUMS} FROM msg WHERE ts >= ?", w).fetchone())
    engines = {
        r[0]: _pack(r[1:]) for r in con.execute(f"SELECT engine, {SUMS} FROM msg WHERE ts >= ? GROUP BY engine", w)
    }
    models = [
        {"motor": r[0], "model": r[1], **_pack(r[2:])}
        for r in con.execute(f"SELECT engine, model, {SUMS} FROM msg WHERE ts >= ? GROUP BY engine, model", w)
    ]
    models.sort(key=lambda m: (-m["usd"], -m["toplam"]))
    sources = [
        {"motor": r[0], "kaynak": r[1] or "—", **_pack(r[2:])}
        for r in con.execute(f"SELECT engine, project, {SUMS} FROM msg WHERE ts >= ? GROUP BY engine, project", w)
    ]
    sources.sort(key=lambda m: (-m["usd"], -m["toplam"]))
    return {"toplam": total, "motor": engines, "model": models, "kaynak": sources[:25]}


def summarize() -> dict[str, Any]:
    con = _db()
    try:
        now = time.time()
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        windows = {
            "5s": _window(con, now - 5 * 3600),
            "bugun": _window(con, today.timestamp()),
            "7g": _window(con, (today - timedelta(days=6)).timestamp()),
            "30g": _window(con, (today - timedelta(days=29)).timestamp()),
            "tum": _window(con, 0),
        }
        first_day = (today - timedelta(days=29)).strftime("%Y-%m-%d")
        daily: dict[str, dict[str, Any]] = {
            (today - timedelta(days=i)).strftime("%Y-%m-%d"): {} for i in range(29, -1, -1)
        }
        for day, model, n, inp, out, cr, cw, usd in con.execute(
            f"SELECT day, model, {SUMS} FROM msg WHERE day >= ? GROUP BY day, model", (first_day,)
        ):
            if day in daily:
                daily[day][model] = {"toplam": (inp or 0) + (out or 0) + (cr or 0) + (cw or 0),
                                     "cikti": out or 0, "usd": round(usd or 0, 4)}
        sessions = []
        for r in con.execute(
            f"SELECT m.session, MIN(m.ts), MAX(m.ts), MAX(m.project), GROUP_CONCAT(DISTINCT m.model), "
            f"COALESCE(MAX(s.title), ''), {SUMS.replace('SUM(', 'SUM(m.')} "
            "FROM msg m LEFT JOIN sess s ON s.session = m.session "
            "WHERE m.session != '' GROUP BY m.session ORDER BY MAX(m.ts) DESC LIMIT 40"
        ):
            sessions.append({
                "oturum": r[0][:8], "basla": datetime.fromtimestamp(r[1]).strftime("%d.%m %H:%M"),
                "son": datetime.fromtimestamp(r[2]).strftime("%d.%m %H:%M"), "sure_dk": round((r[2] - r[1]) / 60),
                "proje": r[3], "modeller": sorted((r[4] or "").split(",")), "baslik": r[5], **_pack(r[6:]),
            })
        first = con.execute("SELECT MIN(ts) FROM msg").fetchone()[0]
        quota = None
        try:
            q = json.loads(AGY_QUOTA_CACHE.read_text(encoding="utf-8"))
            quota = {"zaman": q.get("fetched_at"), "gruplar": [
                {"ad": g.get("displayName"), "kovalar": [
                    {"ad": b.get("displayName"), "kalan": b.get("remainingFraction"), "sifirlanma": b.get("resetTime")}
                    for b in g.get("buckets", [])]}
                for g in (q.get("response") or {}).get("groups", [])]}
        except Exception:
            pass
        claude_quota = None
        try:
            cq = json.loads(CLAUDE_QUOTA_CACHE.read_text(encoding="utf-8"))
            resp = cq.get("response") or {}
            kovalar = []
            for key, label in CLAUDE_QUOTA_KEYS:
                b = resp.get(key)
                if isinstance(b, dict) and b.get("utilization") is not None:
                    used = float(b["utilization"])
                    kovalar.append({"ad": label, "kalan": max(0.0, 100.0 - used) / 100.0,
                                    "kullanilan": used, "sifirlanma": b.get("resets_at")})
            if kovalar:
                claude_quota = {"zaman": cq.get("fetched_at"), "plan": cq.get("plan"), "kovalar": kovalar}
        except Exception:
            pass
        return {
            "zaman": now,
            "ilk_kayit": datetime.fromtimestamp(first).strftime("%d.%m.%Y") if first else None,
            "pencereler": windows,
            "gunluk": [{"gun": d, "modeller": m} for d, m in daily.items()],
            "oturumlar": sessions,
            "agy_kota": quota,
            "claude_kota": claude_quota,
            "fiyatlar": {p: list(v) for p, v in PRICES},
        }
    finally:
        con.close()


def write_summary(data: dict[str, Any]) -> Path:
    target = _summary_file()
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(target)
    return target


def refresh() -> dict[str, Any]:
    try:  # Claude /usage kotası (5 sa + haftalık); 2 dk'dan tazeyse ağa çıkılmaz
        from jrc_usage import collect_claude_quota
        collect_claude_quota(max_age=120)
    except Exception:
        pass
    collect()
    data = summarize()
    write_summary(data)
    return data


def fmt(n: float) -> str:
    n = float(n or 0)
    for lim, suf in ((1e9, "Mr"), (1e6, "M"), (1e3, "B")):
        if n >= lim:
            return f"{n / lim:.2f}{suf}".replace(".", ",")
    return str(int(n))


def print_report(data: dict[str, Any]) -> None:
    c, b, d, r = "\033[36m", "\033[1m", "\033[2m", "\033[0m"
    print(f"\n{b}🔥 Token yakımı — motor & model{r} {d}(defter: {DB_FILE}, ilk kayıt {data.get('ilk_kayit')}){r}")
    for key, label in (("5s", "Son 5 saat"), ("bugun", "Bugün"), ("7g", "Son 7 gün"), ("30g", "Son 30 gün")):
        w = data["pencereler"][key]
        t = w["toplam"]
        print(f"\n{c}{label}{r}  {b}{fmt(t['toplam'])}{r} token · çıktı {fmt(t['cikti'])} · "
              f"{t['cagri']} çağrı · API eşdeğeri ${t['usd']:,.2f}")
        for m in w["model"][:8]:
            print(f"  {m['motor']:<6} {m['model']:<28} {fmt(m['toplam']):>9}  çıktı {fmt(m['cikti']):>8}  "
                  f"girdi {fmt(m['girdi']):>8}  önb.okuma {fmt(m['onbellek_okuma']):>8}  "
                  f"önb.yazma {fmt(m['onbellek_yazma']):>8}  {m['cagri']:>5}x  ${m['usd']:,.2f}")
    print()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    data = refresh()
    if "--json" in argv:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    elif "--sessiz" not in argv:
        print_report(data)
        print(f"\033[2mPanel özeti: {_summary_file()}\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
