"""
Bağlı ürünler (Antigravity `agy`, Claude Code `claude`, Ollaya ve yerel `jrc` yönlendirmeleri)
için kullanım ve kota bilgilerini toplayıp terminalde raporlayan modül.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

AGY_STATE_DIR = Path.home() / ".gemini" / "antigravity-cli"
AGY_AUTH_FILE = AGY_STATE_DIR / "quota_auth.json"
CLAUDE_DIR = Path.home() / ".claude"
CACHE_DIR = Path.home() / ".cache" / "jroute"
AGY_QUOTA_CACHE_FILE = CACHE_DIR / "agy_quota_cache.json"


def format_tokens(count: int) -> str:
    """Token sayısını okunabilir formata çevirir (örn. 1.2M, 45.3K)."""
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.2f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def _format_reset_time(iso_str: str) -> str:
    """ISO zaman damgasını yerel tarih/saat ve kalan süre olarak biçimlendirir."""
    if not iso_str:
        return "Bilinmiyor"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        now = datetime.now(timezone.utc).astimezone()
        diff = local_dt - now
        total_secs = int(diff.total_seconds())
        if total_secs <= 0:
            rel = "yenileniyor"
        else:
            days, rem = divmod(total_secs, 86400)
            hours, rem = divmod(rem, 3600)
            mins = rem // 60
            parts = []
            if days:
                parts.append(f"{days}g")
            if hours:
                parts.append(f"{hours}s")
            if mins and not days:
                parts.append(f"{mins}dk")
            rel = " ".join(parts) if parts else "<1dk"
        return f"{local_dt.strftime('%d.%m %H:%M')} ({rel})"
    except Exception:
        return iso_str


def _format_age(ts: float) -> str:
    """Unix zaman damgasından bu yana geçen süreyi döndürür."""
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return f"{secs}sn önce"
    mins = secs // 60
    if mins < 60:
        return f"{mins}dk önce"
    hours = mins // 60
    if hours < 24:
        return f"{hours}sa önce"
    return f"{hours // 24}g önce"


def _progress_bar(remaining_fraction: float, width: int = 16) -> str:
    """Kalan kota oranı için renkli ilerleme çubuğu üretir."""
    frac = max(0.0, min(1.0, float(remaining_fraction)))
    filled = int(round(frac * width))
    empty = width - filled
    if frac <= 0.10:
        color = "\033[31m"  # kırmızı
    elif frac <= 0.35:
        color = "\033[33m"  # sarı
    else:
        color = "\033[32m"  # yeşil
    return f"{color}{'█' * filled}\033[2m{'░' * empty}\033[0m"


def _query_agy_endpoint(address: str, csrf_token: str) -> dict[str, Any] | None:
    """Antigravity Language Server RetrieveUserQuotaSummary uç noktasını sorgular."""
    if not address or not csrf_token:
        return None
    url = f"http://{address}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": csrf_token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            if resp.status == 200:
                payload = json.loads(resp.read().decode("utf-8"))
                return payload.get("response")
    except Exception:
        return None
    return None


def collect_agy_usage() -> dict[str, Any]:
    """Antigravity (agy) kota ve kullanım bilgilerini toplar."""
    installed = shutil.which("agy") is not None
    candidates: list[tuple[str, str]] = []

    env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    env_csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    if env_addr and env_csrf:
        candidates.append((env_addr, env_csrf))

    if AGY_AUTH_FILE.is_file():
        try:
            auth_data = json.loads(AGY_AUTH_FILE.read_text(encoding="utf-8"))
            f_addr = str(auth_data.get("address", "")).strip()
            f_csrf = str(auth_data.get("csrf_token", "")).strip()
            if f_addr and f_csrf and (f_addr, f_csrf) not in candidates:
                candidates.append((f_addr, f_csrf))
        except Exception:
            pass

    for addr, csrf in candidates:
        response = _query_agy_endpoint(addr, csrf)
        if response:
            snapshot = {"fetched_at": time.time(), "response": response}
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                AGY_QUOTA_CACHE_FILE.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            except Exception:
                pass
            return {
                "installed": installed,
                "status": "live",
                "fetched_at": snapshot["fetched_at"],
                "groups": response.get("groups") or [],
            }

    if AGY_QUOTA_CACHE_FILE.is_file():
        try:
            cached = json.loads(AGY_QUOTA_CACHE_FILE.read_text(encoding="utf-8"))
            resp = cached.get("response") or {}
            return {
                "installed": installed,
                "status": "cached",
                "fetched_at": cached.get("fetched_at", 0.0),
                "groups": resp.get("groups") or [],
            }
        except Exception:
            pass

    return {
        "installed": installed,
        "status": "unavailable",
        "fetched_at": None,
        "groups": [],
    }


def _parse_iso_ts(ts_str: str) -> float | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _empty_bucket() -> dict[str, int]:
    return {
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def _add_usage(bucket: dict[str, int], usage: dict[str, Any]) -> None:
    bucket["turns"] += 1
    bucket["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
    bucket["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
    bucket["cache_read_input_tokens"] += int(usage.get("cache_read_input_tokens", 0) or 0)
    bucket["cache_creation_input_tokens"] += int(usage.get("cache_creation_input_tokens", 0) or 0)


def collect_claude_usage() -> dict[str, Any]:
    """Claude Code oturum loglarından ve stats-cache.json dosyasından kullanım verilerini toplar."""
    installed = shutil.which("claude") is not None
    auth_info: dict[str, Any] = {}
    if installed:
        try:
            proc = subprocess.run(
                ["claude", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                # Çıktının sonundaki olası ekstra karakterleri temizle
                raw = proc.stdout.strip()
                end_idx = raw.rfind("}")
                if end_idx != -1:
                    parsed = json.loads(raw[: end_idx + 1])
                    auth_info = {
                        "loggedIn": parsed.get("loggedIn"),
                        "planTier": parsed.get("planTier") or parsed.get("subscriptionType"),
                        "email": parsed.get("email"),
                    }
        except Exception:
            pass

    now = time.time()
    cutoff_7d = now - 7 * 86400
    cutoff_5h = now - 5 * 3600
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    window_5h = _empty_bucket()
    window_today = _empty_bucket()
    window_7d = _empty_bucket()
    by_model_7d: dict[str, dict[str, int]] = {}

    projects_dir = CLAUDE_DIR / "projects"
    if projects_dir.is_dir():
        seen_ids: set[str] = set()
        for jsonl_path in projects_dir.glob("*/*.jsonl"):
            try:
                if jsonl_path.stat().st_mtime < cutoff_7d:
                    continue
                with jsonl_path.open("r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if '"usage"' not in line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        msg = obj.get("message")
                        if not isinstance(msg, dict) or "usage" not in msg:
                            continue
                        uid = msg.get("id") or obj.get("requestId") or obj.get("uuid")
                        if uid:
                            if uid in seen_ids:
                                continue
                            seen_ids.add(uid)
                        ts = _parse_iso_ts(obj.get("timestamp", ""))
                        if ts is None or ts < cutoff_7d:
                            continue
                        usage = msg["usage"]
                        model = str(msg.get("model") or "unknown")
                        _add_usage(window_7d, usage)
                        model_bucket = by_model_7d.setdefault(model, _empty_bucket())
                        _add_usage(model_bucket, usage)
                        if ts >= today_start:
                            _add_usage(window_today, usage)
                        if ts >= cutoff_5h:
                            _add_usage(window_5h, usage)
            except Exception:
                continue

    all_time: dict[str, Any] = {}
    stats_file = CLAUDE_DIR / "stats-cache.json"
    if stats_file.is_file():
        try:
            stats = json.loads(stats_file.read_text(encoding="utf-8"))
            all_time = {
                "lastComputedDate": stats.get("lastComputedDate"),
                "totalSessions": stats.get("totalSessions", 0),
                "totalMessages": stats.get("totalMessages", 0),
            }
        except Exception:
            pass

    return {
        "installed": installed,
        "auth": auth_info,
        "windows": {
            "5h": window_5h,
            "today": window_today,
            "7d": window_7d,
        },
        "by_model_7d": by_model_7d,
        "all_time": all_time,
    }


def collect_ollaya_usage(base_url: str | None = None) -> dict[str, Any]:
    """Yerel Ollaya yönlendirici sunucusunun durumunu ve yüklü modellerini sorgular."""
    url = (base_url or os.environ.get("TYPESAFE_BASE_URL", "http://127.0.0.1:11435")).rstrip("/")
    online = False
    running_models: list[dict[str, Any]] = []
    try:
        with urllib.request.urlopen(f"{url}/", timeout=1.5) as resp:
            online = resp.status == 200
    except Exception:
        online = False

    if online:
        try:
            with urllib.request.urlopen(f"{url}/api/ps", timeout=1.5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    for m in data.get("models") or []:
                        running_models.append({
                            "name": m.get("name"),
                            "parameter_size": (m.get("details") or {}).get("parameter_size"),
                            "device": m.get("device"),
                        })
        except Exception:
            pass

    return {"url": url, "online": online, "running_models": running_models}


def collect_workspace_jrc_usage(cwd: Path | None = None) -> dict[str, Any]:
    """Mevcut çalışma dizinindeki .jrc_state.json üzerinden JRC yönlendirme istatistiklerini okur."""
    target_dir = cwd or Path.cwd()
    state_file = target_dir / ".jrc_state.json"
    if not state_file.is_file():
        return {"has_state": False, "total": 0, "by_tool": {}}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        history = data.get("task_history") or []
        by_tool: dict[str, int] = {}
        for item in history:
            t = str(item.get("tool") or "unknown")
            by_tool[t] = by_tool.get(t, 0) + 1
        return {
            "has_state": True,
            "project_name": data.get("project_name", target_dir.name),
            "total": len(history),
            "by_tool": by_tool,
        }
    except Exception:
        return {"has_state": False, "total": 0, "by_tool": {}}


def collect_all_usage(cwd: Path | None = None) -> dict[str, Any]:
    """Tüm bağlı ürünlerin kullanım bilgilerini tek sözlükte toplar."""
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "antigravity": collect_agy_usage(),
        "claude": collect_claude_usage(),
        "ollaya": collect_ollaya_usage(),
        "workspace": collect_workspace_jrc_usage(cwd),
    }


def _format_window_line(label: str, bucket: dict[str, int]) -> str:
    turns = bucket["turns"]
    out_t = format_tokens(bucket["output_tokens"])
    in_t = format_tokens(bucket["input_tokens"])
    cache_r = format_tokens(bucket["cache_read_input_tokens"])
    cache_c = format_tokens(bucket["cache_creation_input_tokens"])
    return (
        f"    {label:<12}: \033[1m{turns:>4}\033[0m istek · "
        f"çıktı=\033[36m{out_t}\033[0m · girdi={in_t} · "
        f"önbellek_okuma={cache_r} · önbellek_yazma={cache_c}"
    )


def print_usage_report(cwd: Path | None = None, as_json: bool = False) -> None:
    """Bağlı ürünlerin kullanım ve kota raporunu terminale yazdırır."""
    data = collect_all_usage(cwd)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    bold = "\033[1m"
    cyan = "\033[36m"
    green = "\033[32m"
    yellow = "\033[33m"
    red = "\033[31m"
    magenta = "\033[35m"
    dim = "\033[2m"
    reset = "\033[0m"

    print(f"\n{cyan}╭──────────────────────────────────────────────────────────────────────╮{reset}")
    print(f"{cyan}│{reset} 📊 {bold}JRC Bağlı Ürünler Kullanım & Kota Raporu{reset}")
    print(f"{cyan}╰──────────────────────────────────────────────────────────────────────╯{reset}")

    # 1. Antigravity (agy)
    agy = data["antigravity"]
    if agy["status"] == "live":
        status_badge = f"{green}canlı{reset}"
    elif agy["status"] == "cached":
        age = _format_age(float(agy["fetched_at"] or 0.0))
        status_badge = f"{yellow}önbellek · {age}{reset}"
    else:
        status_badge = f"{red}bağlantı yok{reset}"

    print(f"\n  {cyan}●{reset} {bold}Antigravity (agy){reset} [{status_badge}]")
    if agy["groups"]:
        for grp in agy["groups"]:
            g_name = grp.get("displayName", "Modeller")
            print(f"    {bold}{g_name}{reset}")
            for b in grp.get("buckets") or []:
                window = b.get("window", b.get("bucketId", ""))
                rem_frac = float(b.get("remainingFraction", 0.0))
                pct = rem_frac * 100
                bar = _progress_bar(rem_frac)
                reset_str = _format_reset_time(b.get("resetTime", ""))
                print(
                    f"      {window:<7} {bar} {bold}{pct:5.1f}%{reset} kalan  "
                    f"{dim}· yenilenme: {reset_str}{reset}"
                )
    else:
        print(f"    {dim}Aktif Antigravity Language Server oturumu bulunamadı.{reset}")

    # 2. Claude Code (claude)
    cl = data["claude"]
    auth = cl.get("auth") or {}
    plan = auth.get("planTier")
    plan_badge = f"{green}plan: {plan}{reset}" if plan else (f"{green}kurulu{reset}" if cl["installed"] else f"{red}yok{reset}")
    email = auth.get("email")
    email_str = f" {dim}({email}){reset}" if email else ""
    print(f"\n  {green}●{reset} {bold}Claude Code (claude){reset} [{plan_badge}]{email_str}")
    wins = cl["windows"]
    print(_format_window_line("Son 5 saat", wins["5h"]))
    print(_format_window_line("Bugün", wins["today"]))
    print(_format_window_line("Son 7 gün", wins["7d"]))

    by_model = cl.get("by_model_7d") or {}
    if by_model:
        sorted_models = sorted(
            by_model.items(),
            key=lambda kv: kv[1]["output_tokens"] + kv[1]["input_tokens"],
            reverse=True,
        )
        model_parts = [
            f"{m}: {format_tokens(b['output_tokens'])} çıktı ({b['turns']}x)"
            for m, b in sorted_models[:4]
        ]
        print(f"    {dim}Modeller (7g): {' · '.join(model_parts)}{reset}")

    all_time = cl.get("all_time") or {}
    if all_time:
        print(
            f"    {dim}Toplam özet ({all_time.get('lastComputedDate', '?')} itibarıyla): "
            f"{all_time.get('totalSessions', 0)} oturum · {all_time.get('totalMessages', 0)} mesaj{reset}"
        )

    # 3. Ollaya (yerel karar motoru)
    ol = data["ollaya"]
    ol_badge = f"{green}çalışıyor{reset}" if ol["online"] else f"{yellow}kapalı{reset}"
    models_str = ", ".join(
        f"{m['name']} ({m.get('parameter_size') or '?'}, {m.get('device') or 'cpu'})"
        for m in ol["running_models"]
    )
    print(f"\n  {magenta}●{reset} {bold}Ollaya (Jev Karar Motoru){reset} [{ol_badge}] {dim}{ol['url']}{reset}")
    if models_str:
        print(f"    Yüklü model : {bold}{models_str}{reset}")
    elif ol["online"]:
        print(f"    {dim}Aktif bellekte yüklü model yok (ilk istekte yüklenir).{reset}")

    # 4. Mevcut dizin JRC yönlendirme özeti
    ws = data["workspace"]
    if ws.get("has_state") and ws.get("total", 0) > 0:
        tools_summary = " · ".join(f"{k}={v}" for k, v in sorted(ws["by_tool"].items()))
        print(
            f"\n  {yellow}●{reset} {bold}Bu Dizin (JRC Geçmişi){reset}: "
            f"{ws['total']} kayıtlı görev ({tools_summary})"
        )

    print()
