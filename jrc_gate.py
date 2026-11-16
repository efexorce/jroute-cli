"""JRC Geçidi (jrc_gate) — tüm bot/servis LLM çağrılarının TEK karar noktası.

Kural: peribot, geocubot, eretnabot, Hermes ve diğer servisler `claude` / `agy`
motorunu kendi başına seçemez. Hangi araç, hangi model, hangi efor kullanılacağı
burada (jrc) belirlenir; her karar `~/.cache/jroute/gate.jsonl` defterine yazılır.

Yalnızca standart kütüphane kullanır: odoo / hermes venv'leri içinden de
içe aktarılabilir. Görev analizi gerektiğinde jev venv'indeki `router.py`
(Ollaya/largha) alt süreç olarak çağrılır; yerel olduğu için cloud token harcamaz.

Kullanım (çağıran servis):
    import jrc_gate
    karar = jrc_gate.decide("geocubot", "müşteri mesajı ...", effort_hint="low")
    # karar["tool"], karar["model"], karar["effort"], karar["tag"]
    ... çalıştır ...
    jrc_gate.finish(karar, ok=True, seconds=3.2)
    # kota/hata olursa motoru servis değil jrc değiştirir:
    alt = jrc_gate.fallback(karar, "kota")

Politika: jrc_policy.json (kaynak başına sabitleme `pin`, `max_effort`,
`allow_tools`; global `engine`: auto|claude|agy).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from jrc_profiles import EFFORT_ORDER, ROUTE_PROFILES, agy_model_for  # noqa: E402

def _ledger_file() -> Path:
    if os.environ.get("JRC_LEDGER_FILE"):
        return Path(os.environ["JRC_LEDGER_FILE"])
    extra = Path("/mnt/extra-addons/.jrc_ledger.jsonl")
    if extra.parent.exists() and extra.parent.is_dir():
        return extra
    return Path.home() / ".cache" / "jroute" / "gate.jsonl"


def _policy_file() -> Path:
    if os.environ.get("JRC_POLICY_FILE"):
        return Path(os.environ["JRC_POLICY_FILE"])
    extra = Path("/mnt/extra-addons/jrc_policy.json")
    if extra.exists():
        return extra
    return HERE / "jrc_policy.json"


POLICY_FILE = _policy_file()
LEDGER_FILE = _ledger_file()
ROUTER_PY = HERE / "router.py"
JEV_PYTHON = HERE / ".venv" / "bin" / "python3"

DEFAULT_POLICY: dict[str, Any] = {
    "engine": "auto",
    "analysis_timeout_s": 20,
    "fallback_route": "agy_argon",
    "default": {"max_effort": "high"},
    "sources": {
        # Herkese açık müşteri botu: araçsız kısa sohbet → analiz gereksiz, Haiku/low sabit.
        "geocubot": {"pin": "claude_haiku", "effort": "low", "max_effort": "low"},
        "geocubot_yedek": {"pin": "claude_haiku", "effort": "low", "max_effort": "low"},
        # Küçük yardımcı işler (masraf ayrıştırma, ürün açıklaması): zaten Haiku, araçsız.
        "finansci": {"pin": "claude_haiku", "effort": "low", "max_effort": "medium"},
        "urun_aciklama": {"pin": "claude_haiku", "effort": "low", "max_effort": "medium"},
        # Hermes + Peri (peribot) argon proxy üzerinden gelir; her istekte analiz yapılmaz.
        "proxy": {"pin": "agy_argon", "effort": "low", "max_effort": "high"},
        "hermes": {"pin": "agy_argon", "effort": "low", "max_effort": "high"},
        "peribot": {"pin": "agy_argon", "effort": "medium", "max_effort": "high"},
        # Yönetim ürün kartı vitrin görseli (showcase): Antigravity / Gemini görsel motoru
        "showcase": {"pin": "agy_argon", "effort": "low", "max_effort": "high"},
        # Yönetim ürün kartı fotoğraf düzenleme & oluşturma (foto_duzenle): Antigravity / Gemini görsel motoru
        "foto_duzenle": {"pin": "agy_argon", "effort": "low", "max_effort": "high"},
    },
}

# Araç değişince aynı "kademe"nin karşılığı: (claude, agy)
_TIERS = [("claude_haiku", "agy_flash"), ("claude_sonnet", "agy_argon"), ("claude_opus", "agy_hydrogen")]
_CLAUDE_TO_AGY = {**{c: a for c, a in _TIERS}, "claude_fable": "agy_hydrogen"}
_AGY_TO_CLAUDE = {a: c for c, a in _TIERS}


# ---------------------------------------------------------------- politika

def load_policy() -> dict[str, Any]:
    pol = json.loads(json.dumps(DEFAULT_POLICY))
    pf = _policy_file()
    try:
        user = json.loads(pf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return pol
    for key, val in user.items():
        if key == "sources" and isinstance(val, dict):
            pol["sources"].update(val)
        elif key == "default" and isinstance(val, dict):
            pol["default"].update(val)
        else:
            pol[key] = val
    return pol


def save_policy(pol: dict[str, Any]) -> None:
    _policy_file().write_text(json.dumps(pol, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def set_engine(engine: str) -> str:
    """Global motor kilidi: auto (jrc seçer) | claude | agy."""
    engine = {"argon": "agy", "otomatik": "auto"}.get(engine, engine)
    if engine not in ("auto", "claude", "agy"):
        raise ValueError("engine: auto|claude|agy")
    pf = _policy_file()
    try:
        user = json.loads(pf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        user = {}
    user["engine"] = engine
    save_policy(user)
    return engine


def _source_policy(pol: dict[str, Any], source: str) -> dict[str, Any]:
    merged = dict(pol.get("default", {}))
    merged.update(pol.get("sources", {}).get(source, {}))
    return merged


# ---------------------------------------------------------------- yardımcılar

def tag(decision: dict[str, Any]) -> str:
    return f"[jrc:{decision['tool']}:{decision['model']}:{decision['effort']}]"


def _clamp_effort(effort: str, max_effort: str | None) -> str:
    if effort not in EFFORT_ORDER:
        effort = "medium"
    if max_effort in EFFORT_ORDER and EFFORT_ORDER.index(effort) > EFFORT_ORDER.index(max_effort):
        return max_effort
    return effort


def _from_profile(route_key: str, effort: str | None = None) -> dict[str, Any]:
    prof = ROUTE_PROFILES[route_key]
    eff = effort if effort in EFFORT_ORDER else prof["default_effort"]
    model = prof["model"]
    if prof["tool"] == "agy":
        model = agy_model_for(model, eff)
    return {
        "tool": prof["tool"],
        "model": model,
        "effort": eff,
        "route_key": route_key,
        "label": prof["label"],
    }


def _remap_tool(decision: dict[str, Any], tool: str) -> dict[str, Any]:
    """Karar farklı araca düşüyorsa aynı kademenin karşılığına çevirir."""
    if decision["tool"] == tool:
        return decision
    table = _CLAUDE_TO_AGY if tool == "agy" else _AGY_TO_CLAUDE
    route = table.get(decision["route_key"], "agy_argon" if tool == "agy" else "claude_sonnet")
    out = _from_profile(route, decision["effort"])
    out.update({k: v for k, v in decision.items() if k not in out})
    return out


def _analyze(task: str, cwd: Path, forced_tool: str | None, timeout: float) -> dict[str, Any]:
    """router.py --json (Ollaya/largha, yerel) ile görev analizi."""
    py = str(JEV_PYTHON) if JEV_PYTHON.exists() else sys.executable
    cmd = [py, str(ROUTER_PY), "--json", "--cwd", str(cwd)]
    if forced_tool:
        cmd += ["-t", forced_tool]
    cmd.append(task)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError((p.stderr or "router boş çıktı")[-300:])
    return json.loads(p.stdout)


def _log(entry: dict[str, Any]) -> None:
    try:
        lf = _ledger_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with lf.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- ana API

def decide(
    source: str,
    task: str = "",
    cwd: str | Path | None = None,
    forced_tool: str | None = None,
    effort_hint: str | None = None,
    analyze: bool | None = None,
    route: str | None = None,
) -> dict[str, Any]:
    """Bir servis için araç/model/efor kararı verir (tek geçit).

    analyze=None → politika belirler: `pin` varsa analiz yok (0 gecikme, 0 token);
    yoksa Ollaya ile analiz. Bir önceden verilmiş [jrc:...] etiketi varsa
    (ör. eretnabot'un kararı) tekrar analiz edilmez, yalnızca politikadan geçirilir.
    """
    pol = load_policy()
    sp = _source_policy(pol, source)
    t0 = time.perf_counter()
    cwd_path = Path(cwd or Path.cwd()).resolve()

    engine = pol.get("engine", "auto")
    tool_lock = forced_tool or (engine if engine in ("claude", "agy") else None)
    allow = sp.get("allow_tools")
    if allow and len(allow) == 1:
        tool_lock = tool_lock or allow[0]

    decision: dict[str, Any] | None = None
    mode = "analiz"

    m = re.search(r"\[jrc:([^:\]]+):([^:\]]+):([^\]]+)\]", task or "")
    pin = route if route in ROUTE_PROFILES else sp.get("pin")
    if m and analyze is not True:
        tool, model, eff = m.groups()
        rk = next((k for k, v in ROUTE_PROFILES.items() if v["tool"] == tool and model.startswith(v["model"].rsplit("-", 1)[0])), None)
        if rk:
            decision = _from_profile(rk, eff)
            decision["model"] = model if tool == "agy" else decision["model"]
            mode = "etiket"
    if decision is None and pin and analyze is not True:
        decision = _from_profile(pin, effort_hint or sp.get("effort"))
        mode = "pin"
    if decision is None and analyze is False:
        decision = _from_profile(pol.get("fallback_route", "agy_argon"), effort_hint or sp.get("effort"))
        mode = "varsayilan"
    if decision is None:
        try:
            raw = _analyze((task or "")[:4000], cwd_path, tool_lock, float(pol.get("analysis_timeout_s", 20)))
            decision = {k: raw[k] for k in ("tool", "model", "effort", "route_key", "label")}
            decision["metrics"] = raw.get("metrics", {})
        except Exception as exc:  # Ollaya/router erişilemez → politika varsayılanı
            decision = _from_profile(pol.get("fallback_route", "agy_argon"), effort_hint)
            decision["analysis_error"] = str(exc)[:200]
            mode = "varsayilan"

    if tool_lock:
        decision = _remap_tool(decision, tool_lock)
    elif allow and decision["tool"] not in allow:
        decision = _remap_tool(decision, allow[0])

    decision["effort"] = _clamp_effort(decision["effort"], sp.get("max_effort"))
    if decision["tool"] == "agy":
        decision["model"] = agy_model_for(decision["model"], decision["effort"])

    decision.update(
        source=source,
        mode=mode,
        engine_lock=tool_lock or "auto",
        latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        ts=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    decision["tag"] = tag(decision)
    _log({"type": "decision", "ts": decision["ts"], "source": source, "tool": decision["tool"],
          "model": decision["model"], "effort": decision["effort"], "mode": mode,
          "lock": decision["engine_lock"], "ms": decision["latency_ms"],
          "task": " ".join((task or "").split())[:120]})
    return decision


def record(source: str, decision: dict[str, Any], task: str = "") -> None:
    """jrc/jroute'un kendi (insan) çağrılarını da aynı deftere yazar."""
    _log({"type": "decision", "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source,
          "tool": decision["tool"], "model": decision["model"], "effort": decision["effort"],
          "mode": "analiz", "lock": decision.get("tool"), "ms": decision.get("latency_ms", 0),
          "task": " ".join((task or "").split())[:120]})


def fallback(decision: dict[str, Any], reason: str) -> dict[str, Any] | None:
    """Kota/hata durumunda motoru servis değil jrc değiştirir. Tek seferlik; döngü yok."""
    if decision.get("fallback_of"):
        return None
    pol = load_policy()
    if pol.get("engine") in ("claude", "agy"):
        return None  # kullanıcı motoru kilitlemiş: sessizce başka motora geçme
    sp = _source_policy(pol, decision.get("source", ""))
    other = "agy" if decision["tool"] == "claude" else "claude"
    allow = sp.get("allow_tools")
    if allow and other not in allow:
        return None
    alt = _remap_tool(dict(decision), other)
    alt["effort"] = _clamp_effort(alt["effort"], sp.get("max_effort"))
    if alt["tool"] == "agy":
        alt["model"] = agy_model_for(alt["model"], alt["effort"])
    alt.update(fallback_of=decision["tag"], mode="fallback", ts=time.strftime("%Y-%m-%d %H:%M:%S"))
    alt["tag"] = tag(alt)
    _log({"type": "fallback", "ts": alt["ts"], "source": alt.get("source"), "from": decision["tag"],
          "to": alt["tag"], "reason": reason})
    return alt


def finish(decision: dict[str, Any], ok: bool, seconds: float = 0.0, note: str = "") -> None:
    _log({"type": "result", "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "source": decision.get("source"),
          "tag": decision.get("tag"), "ok": bool(ok), "s": round(seconds, 2), "note": note[:120]})


# ---------------------------------------------------------------- rapor / denetim

def ledger_summary(hours: float = 24.0) -> dict[str, Any]:
    cutoff = time.time() - hours * 3600
    rows: list[dict[str, Any]] = []
    try:
        for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if time.mktime(time.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")) >= cutoff:
                    rows.append(r)
            except (ValueError, KeyError):
                continue
    except OSError:
        pass
    by: dict[str, dict[str, int]] = {}
    fb = 0
    for r in rows:
        if r["type"] == "decision":
            d = by.setdefault(r["source"], {})
            key = f"{r['tool']}:{r['model']}"
            d[key] = d.get(key, 0) + 1
        elif r["type"] == "fallback":
            fb += 1
    return {"hours": hours, "decisions": sum(1 for r in rows if r["type"] == "decision"),
            "fallbacks": fb, "by_source": by}


_DIRECT = re.compile(r"(\.npm-global/bin/claude|\.local/bin/claude|\.local/bin/agy|AGY_BIN\s*=|\[\s*CLAUDE\s*,|\"claude\"\s*,\s*\"-p\")")
_GATED = re.compile(r"(jrc_gate|claude_calistir|argon_calistir|jrc_koprusu)")


def audit(roots: list[str | Path] | None = None) -> list[dict[str, Any]]:
    """claude/agy'yi doğrudan çağıran ve geçide bağlı OLMAYAN dosyaları listeler."""
    roots = roots or [Path.home() / "Desktop/projeler/odoo", Path.home() / ".hermes/plugins"]
    found: list[dict[str, Any]] = []
    for root in roots:
        for path in Path(root).rglob("*.py"):
            if any(p in path.parts for p in (".venv", "node_modules", "__pycache__", "moduller", "addons")):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            hits = [i for i, ln in enumerate(text.splitlines(), 1) if _DIRECT.search(ln) and not ln.lstrip().startswith("#")]
            if hits:
                found.append({"file": str(path), "lines": hits[:6], "gated": bool(_GATED.search(text))})
    return sorted(found, key=lambda r: (r["gated"], r["file"]))


if __name__ == "__main__":
    print(json.dumps(ledger_summary(), ensure_ascii=False, indent=2))
