#!/home/efe/Desktop/jev/.venv/bin/python3
"""
Jev / Ollaya Üst Router Betiği (Claude Code & Antigravity CLI)

Terminalden verilen görevi (ve çalışma dizini bağlamını) yerel Ollaya (Jev API)
üzerinden TypeSafe SDK ile tek bir `system_one` çağrısında analiz eder;
hem en uygun aracı (`claude` veya `agy`) hem de alt modeli ve efor seviyesini
seçerek ilgili CLI komutunu başlatır.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import jrc_context
from jrc_profiles import ROUTE_PROFILES, agy_model_for
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

# Claude'a emrin JEV router'dan geldiğini bildiren bağlam (ayrıntı: ~/.claude/CLAUDE.md "JEV" bölümü).
JEV_SYSTEM_PROMPT = (
    "Bu emir JEV router'dan (jroute/jrc) geldi; kullanıcı Mustafa'nın JEV iş akışıdır. "
    "/home/efe çöplük değil, JEV klasörüdür; bu tür işlemlerde kullanılır. "
    "İzinsiz taşıma/silme/temizlik yapma. Dizin haritası ve kurallar için ~/.claude/CLAUDE.md içindeki JEV bölümüne bak. "
    "Bu projede Git KULLANILMAMAKTADIR. Projenin ortak hafıza katmanı, görev geçmişi, son değişen dosyalar "
    "ve mimari kararlar proje kökündeki '.jrc_context.md' dosyasında tutulur. Göreve başlamadan önce .jrc_context.md "
    "içeriğini oku, bağlamı koru"
)
# Yönetimsiz akışta (ya da yürütücü yönetici modelin (Opus) kendisiyken) notu yürütücü yazar;
# yönetimli akışta bu cümle yerine jrc_director.executor_brief() eklenir.
JEV_SELF_NOTE_PROMPT = " ve aldığın kritik mimari kararları bu dosyadaki 'Ortak Notlar' bölümüne ekle."
os.environ["JEV_ORIGIN"] = "1"  # exec/subprocess ile çocuk sürece geçer

DEFAULT_BASE_URL = os.environ.get("TYPESAFE_BASE_URL", "http://127.0.0.1:11435")
DEFAULT_API_KEY = os.environ.get("TYPESAFE_API_KEY", "local")
DEFAULT_DECISION_MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "largha")

# Route profilleri jrc_profiles.py içinde (stdlib-only, jrc_gate ile ortak)


def ensure_ollaya_running(base_url: str) -> None:
    """Yerel Ollaya sunucusunun ayakta olduğundan emin olur."""
    if not ("127.0.0.1" in base_url or "localhost" in base_url):
        return
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/", timeout=1.5) as resp:
            if resp.status == 200:
                return
    except Exception:
        pass

    ollaya_bin = shutil.which("ollaya") or "/home/efe/.local/bin/ollaya"
    if os.path.exists(ollaya_bin):
        subprocess.Popen(
            [ollaya_bin, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(15):
            time.sleep(0.3)
            try:
                with urllib.request.urlopen(f"{base_url.rstrip('/')}/", timeout=1.0) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                continue


def collect_workspace_context(cwd: Path) -> dict[str, Any]:
    """Çalışma klasöründeki proje tipini, dosyaları ve ortak hafızayı (.jrc_context.md) özetler."""
    try:
        entries = sorted(p.name for p in cwd.iterdir() if not p.name.startswith("."))[:25]
    except Exception:
        entries = []

    markers = []
    for name, label in [
        ("package.json", "node/js"),
        ("pyproject.toml", "python"),
        ("requirements.txt", "python"),
        ("Cargo.toml", "rust"),
        ("go.mod", "go"),
        ("docker-compose.yml", "docker"),
        ("odoo", "odoo"),
    ]:
        if (cwd / name).exists():
            markers.append(label)

    # Ortak hafıza katmanı kontrolü (.jrc_context.md)
    mem_summary = jrc_context.get_workspace_memory_summary(cwd)
    if mem_summary["has_memory"]:
        markers.append("hafıza-aktif")

    return {
        "cwd": str(cwd),
        "project_name": cwd.name,
        "stack_markers": markers,
        "top_files": entries,
        "memory": mem_summary,
    }


def detect_explicit_route_override(task: str) -> str | None:
    """Kullanıcı görev içinde açıkça belirli bir alt model istediyse (örn. 'işi fable yapsın', 'opus ile') algılar."""
    lower = task.lower()
    # Açık istek kalıpları (işi X / X ile / X yapsın). Birden çok model geçiyorsa metinde
    # en önce geçen açık kalıp kazanır; çıplak "fable" sözcüğü yalnızca en son denenir
    # (ör. "işi opus yapsın, fable kotası…" → opus).
    patterns: list[tuple[str, str]] = [
        (r"\b(fable\s+(ile|yapsın|yap|kullan|çalıştır)|işi\s+fable|use\s+fable|model\s+fable)\b", "claude_fable"),
        (r"\b(opus\s+(ile|yapsın)|işi\s+opus|use\s+opus)\b", "claude_opus"),
        (r"\b(sonnet\s+(ile|yapsın)|işi\s+sonnet|use\s+sonnet)\b", "claude_sonnet"),
        (r"\b(haiku\s+(ile|yapsın)|işi\s+haiku|use\s+haiku)\b", "claude_haiku"),
        (r"\b(argon\s+(ile|yapsın)|işi\s+argon|use\s+argon)\b", "agy_argon"),
        (r"\b(carbon\s+(ile|yapsın)|işi\s+carbon|use\s+carbon)\b", "agy_carbon"),
        (r"\b(hydrogen\s+(ile|yapsın)|işi\s+hydrogen|use\s+hydrogen)\b", "agy_hydrogen"),
    ]
    best: tuple[int, str] | None = None
    for pat, route_key in patterns:
        m = re.search(pat, lower)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), route_key)
    if best:
        return best[1]
    if re.search(r"\bfable\b", lower):
        return "claude_fable"
    return None


def detect_explicit_tool_override(task: str, forced_tool: str | None) -> str | None:
    """Kullanıcı görev içinde açıkça 'claude ile' veya 'agy ile' dediyse algılar."""
    if forced_tool in ("claude", "agy"):
        return forced_tool
    explicit_route = detect_explicit_route_override(task)
    if explicit_route and explicit_route in ROUTE_PROFILES:
        return ROUTE_PROFILES[explicit_route]["tool"]
    lower = task.lower()
    if re.search(r"\b(claude\s+(ile|yapsın)|işi\s+claude|use\s+claude|claude\s+code\s+ile)\b", lower):
        return "claude"
    if re.search(r"\b(agy\s+(ile|yapsın)|işi\s+agy|antigravity\s+ile|use\s+agy|gemini\s+(ile|yapsın))\b", lower):
        return "agy"
    return None


def analyze_task_with_jev(
    task: str,
    cwd: Path,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = DEFAULT_API_KEY,
    decision_model: str = DEFAULT_DECISION_MODEL,
    forced_tool: str | None = None,
) -> dict[str, Any]:
    """TypeSafe Jev SDK üzerinden tek adımda görev analizi ve model seçimi yapar."""
    # Eğer çağıran çalışma klasörü (örn. odoo / eretnabot) için jrc_policy.json'da pin tanımlıysa
    # ve kullanıcı görev içinde açıkça başka bir model/araç istemediyse doğrudan pin rotasını döndür.
    if forced_tool is None and detect_explicit_route_override(task) is None and detect_explicit_tool_override(task, None) is None:
        try:
            import jrc_gate
            pol = jrc_gate.load_policy()
            sources = pol.get("sources") or {}
            # odoo çalışma dizininden gelen eretnabot çağrıları
            src_cfg = sources.get("eretnabot") if "odoo" in cwd.parts or cwd.name == "odoo" else None
            pin_key = (src_cfg or {}).get("pin")
            if pin_key and pin_key in ROUTE_PROFILES:
                pin_eff = (src_cfg or {}).get("effort") or ROUTE_PROFILES[pin_key]["default_effort"]
                pin_key, sub_from = jrc_gate.substitute_route(pin_key, pol)
                prof = ROUTE_PROFILES[pin_key]
                f_tool = prof["tool"]
                f_model = agy_model_for(prof["model"], pin_eff) if f_tool == "agy" else prof["model"]
                lbl = prof["label"]
                if sub_from:
                    lbl += f" · {sub_from.split('_', 1)[-1]}→{pin_key.split('_', 1)[-1]} (kota ikamesi)"
                return {
                    "tool": f_tool,
                    "model": f_model,
                    "effort": pin_eff,
                    "route_key": pin_key,
                    "label": lbl,
                    "substituted_from": sub_from,
                    "latency_ms": 0.0,
                    "decision_model": "policy_pin",
                    "metrics": {},
                }
        except Exception:
            pass

    ensure_ollaya_running(base_url)
    ws = collect_workspace_context(cwd)

    route_criteria = {k: v["criteria"] for k, v in ROUTE_PROFILES.items()}

    t0 = time.perf_counter()
    # Python 3.14+ stdlib zstd içerdiğinden SDK gövdeyi varsayılan olarak sıkıştırıyor; yerel Ollaya zstd açmıyor → 400 invalid JSON.
    with TypeSafeClient(base_url=base_url, api_key=api_key, compression=False) as client:
        # largha'nın bağlamı çok dar: sorular + kriterler bütçenin çoğunu kullanıyor.
        # Önce kompakt state ile dene; 422 "dropped to fit the context" gelirse daha da kısalt.
        last_exc: Exception | None = None
        for task_limit, with_ws in STATE_BUDGETS:
            state = build_decision_state(task, ws, task_limit, with_ws)
            try:
                response = _ask_jev(client, decision_model, state, route_criteria)
                break
            except Exception as exc:  # TypeSafeAPIError
                if "dropped to fit the context" not in str(exc):
                    raise
                last_exc = exc
        else:
            raise last_exc  # type: ignore[misc]
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    return _build_decision(task, forced_tool, response, elapsed_ms)


# (görev karakter limiti, workspace özeti eklensin mi) — sırayla denenir
STATE_BUDGETS: list[tuple[int, bool]] = [(700, True), (400, False), (150, False)]


def _clip(text: str, limit: int) -> str:
    """Uzun metni baş + son kısmı koruyarak kırpar."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head - 3
    return text[:head] + " … " + text[-tail:]


def build_decision_state(task: str, ws: dict[str, Any], task_limit: int, with_ws: bool) -> dict[str, Any]:
    """Karar modeline gidecek minimal state (dosya listesi / eski görevler gönderilmez)."""
    state: dict[str, Any] = {"task": _clip(task, task_limit)}
    if with_ws:
        state["project"] = ws.get("project_name", "")
        state["stack"] = ws.get("stack_markers", [])
    return state


def _ask_jev(client: Any, decision_model: str, state: dict[str, Any], route_criteria: dict[str, str]) -> Any:
    return client.system_one(
            model=decision_model,
            state=state,
            questions={
                "route": Choice(
                    instructions=(
                        "Select the best CLI agent and sub-model profile for this developer task."
                    ),
                    criteria=route_criteria,
                ),
                "tool": Choice(
                    instructions=(
                        "Which CLI tool is better suited for this task: claude (Claude Code for focused code editing, "
                        "git commits, refactoring, backend/database debugging) or agy (Antigravity for full-stack web, "
                        "UI design, multi-step workspace automation, research, and general tasks)?"
                    ),
                    criteria={
                        "claude": "Focused code editing, git/commit workflows, refactoring, tests, or backend/ORM debugging",
                        "agy": "Full-stack web, UI/UX design, system automation, research, bots, or multi-step orchestration",
                    },
                ),
                "complexity": Score(
                    instructions="How complex and reasoning-intensive is this task?",
                    criteria=[
                        "Low: Simple lookup, status check, git commit/push, or trivial one-line change",
                        "Medium: Standard feature, bugfix, UI update, or single-script creation",
                        "High: Complex multi-file implementation, deep debugging, or architectural change",
                        "XHigh: Critical production system overhaul, deep root-cause analysis, or massive refactor",
                    ],
                ),
                "is_critical": Noul(
                    instructions="Does this task affect production databases, live servers, payments, or high-risk code?",
                    criteria={
                        "true": "Touches production, databases, live deployment, or critical business logic",
                        "false": "Safe local task, UI tweak, read-only query, or routine development",
                    },
                ),
            },
        )


def _build_decision(task: str, forced_tool: str | None, response: Any, elapsed_ms: float) -> dict[str, Any]:
    route_ans = response.choices["route"]
    tool_ans = response.choices["tool"]
    comp_ans = response.scores["complexity"]
    crit_ans = response.nouls["is_critical"]

    chosen_route_key = route_ans.choice
    route_probs: dict[str, float] = dict(route_ans.probabilities)
    tool_probs: dict[str, float] = dict(tool_ans.probabilities)
    raw_score = float(comp_ans.score)
    if raw_score < 0.85:
        comp_level = "low"
    elif raw_score < 1.75:
        comp_level = "medium"
    elif raw_score < 2.40:
        comp_level = "high"
    else:
        comp_level = "xhigh"
    crit_prob = float(crit_ans.noul)

    # Kullanıcı açıkça model veya araç belirttiyse öncelik ver
    explicit_route = detect_explicit_route_override(task) if forced_tool is None else None
    explicit_tool = detect_explicit_tool_override(task, forced_tool)
    if explicit_route and explicit_route in ROUTE_PROFILES:
        chosen_route_key = explicit_route
        target_tool = ROUTE_PROFILES[explicit_route]["tool"]
    else:
        target_tool = explicit_tool or (
            tool_ans.choice
            if tool_ans.confidence >= 0.65 and ROUTE_PROFILES[chosen_route_key]["tool"] != tool_ans.choice
            else ROUTE_PROFILES[chosen_route_key]["tool"]
        )

    # Eğer hedef araç chosen_route_key ile uyuşmuyorsa o aracın en uygun alt modelini seç
    if ROUTE_PROFILES[chosen_route_key]["tool"] != target_tool:
        candidates = [k for k, v in ROUTE_PROFILES.items() if v["tool"] == target_tool]
        chosen_route_key = max(candidates, key=lambda k: route_probs.get(k, 0.0))

    # Karmaşıklık ve kritiklik skoruna göre alt model / efor kalibrasyonu
    effort = comp_level
    if crit_prob >= 0.75 and effort in ("low", "medium"):
        effort = "high"

    # Çok basit görevlerde (ör. durum kontrolü, hafıza sorgusu) hafif alt modeli tercih et
    lower_task = task.lower().strip()
    is_quick_task = bool(
        re.fullmatch(
            r"(git\s+)?(status|durum|hafıza|kontrol|özet|liste|listele)(\s+et)?",
            lower_task,
        )
    )
    if is_quick_task:
        effort = "low"
        chosen_route_key = "claude_haiku" if target_tool == "claude" else "agy_flash"
    elif effort == "xhigh" or (effort == "high" and crit_prob >= 0.70):
        # Çok yüksek karmaşıklıkta ilgili aracın en güçlü modeline yükselt
        if target_tool == "claude" and chosen_route_key == "claude_haiku":
            chosen_route_key = "claude_opus"
        elif target_tool == "agy" and chosen_route_key == "agy_flash":
            chosen_route_key = "agy_argon"

    # Kota ikamesi (jrc_policy.json → route_substitute, ör. fable→opus): son rota belirlendikten sonra
    substituted_from: str | None = None
    try:
        import jrc_gate
        chosen_route_key, substituted_from = jrc_gate.substitute_route(chosen_route_key)
    except Exception:
        pass

    profile = ROUTE_PROFILES[chosen_route_key]
    final_tool = profile["tool"]
    final_model = profile["model"]
    label = profile["label"]
    if substituted_from:
        label += f" · {substituted_from.split('_', 1)[-1]}→{chosen_route_key.split('_', 1)[-1]} (kota ikamesi)"

    # agy model isimlerinde efor soneki uyumu
    if final_tool == "agy":
        final_model = agy_model_for(final_model, effort)

    return {
        "tool": final_tool,
        "model": final_model,
        "effort": effort,
        "route_key": chosen_route_key,
        "label": label,
        "substituted_from": substituted_from,
        "latency_ms": elapsed_ms,
        "decision_model": response.model,
        "metrics": {
            "route_confidence": round(route_ans.confidence, 3),
            "route_probabilities": {k: round(v, 3) for k, v in route_probs.items()},
            "tool_probabilities": {k: round(v, 3) for k, v in tool_probs.items()},
            "complexity_level": comp_level,
            "complexity_score": round(comp_ans.score, 3),
            "critical_probability": round(crit_prob, 3),
        },
    }


def build_cli_command(
    decision: dict[str, Any],
    task: str,
    print_mode: bool = False,
    skip_permissions: bool = False,
    extra_args: list[str] | None = None,
    brief: str | None = None,
    allowed_user_id: int | None = None,
    system_prompt: str | None = None,
) -> list[str]:
    """Seçilen araç ve alt modele göre çalıştırılacak CLI komut dizisini üretir.

    brief: yönetici (Opus) iş emri + hafıza kuralı; verilirse yürütücü notu kendisi yazmaz.
    """
    tool = decision["tool"]
    model = decision["model"]
    effort = decision["effort"]
    extra = extra_args or []
    base_sys_prompt = system_prompt if system_prompt is not None else JEV_SYSTEM_PROMPT

    if tool == "claude":
        cmd = ["claude", "--model", model, "--effort", effort,
               "--append-system-prompt", base_sys_prompt + (f".{brief}" if brief else JEV_SELF_NOTE_PROMPT)]
        if skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if print_mode:
            cmd.append("-p")
        cmd.extend(extra)
        cmd.append(jrc_context.resolve_emir_refs(task, allowed_user_id=allowed_user_id))
        return cmd

    # tool == "agy"
    cmd = ["agy", "--model", model, "--effort", effort]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    base_task = task if ".jrc_context.md" in task else f"[Hafıza: .jrc_context.md] {task}"
    task_payload = jrc_context.resolve_emir_refs(base_task, allowed_user_id=allowed_user_id)
    if brief:
        task_payload += brief
    if print_mode:
        cmd.extend(["-p", task_payload])
    else:
        cmd.extend(["-i", task_payload])
    cmd.extend(extra)
    return cmd


def print_banner(decision: dict[str, Any], cmd: list[str], cwd: Path | None = None) -> None:
    """Seçim özetini terminale renkli ve okunaklı biçimde yazdırır."""
    bold = "\033[1m"
    cyan = "\033[36m"
    green = "\033[32m"
    yellow = "\033[33m"
    dim = "\033[2m"
    reset = "\033[0m"

    target_dir = cwd or Path.cwd()
    mem = jrc_context.get_workspace_memory_summary(target_dir)
    mem_tag = f" · {mem['task_count']} görev kayıtlı" if mem["has_memory"] else " · yeni başlatıldı"

    m = decision["metrics"]
    tool_badge = f"{green}claude{reset}" if decision["tool"] == "claude" else f"{cyan}agy{reset}"
    print(
        f"{bold}⚡ Jev Router{reset} {dim}[{decision['decision_model']} · {decision['latency_ms']} ms]{reset}\n"
        f"  ├─ Araç & Model : {tool_badge} → {bold}{decision['model']}{reset} ({decision['label']})\n"
        f"  ├─ Efor & Risk  : effort={yellow}{decision['effort']}{reset} | "
        f"karmaşıklık={m['complexity_level']} ({m['complexity_score']}) | "
        f"kritiklik={m['critical_probability']}\n"
        f"  ├─ Ortak Hafıza : {green}.jrc_context.md{reset} {dim}[Git-free{mem_tag}]{reset}\n"
        f"  └─ Komut        : {dim}{' '.join(
            [repr(a[:60] + ('…' if len(a) > 60 else '')) if ' ' in a else a for a in cmd]
        )}{reset}",
        file=sys.stderr,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="jroute",
        description="Jev / Ollaya tabanlı Claude Code & Antigravity (agy) Üst Router",
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="Analiz edilip çalıştırılacak görev / komut metni",
    )
    parser.add_argument(
        "-p",
        "--print",
        dest="print_mode",
        action="store_true",
        help="İlgili aracı tek seferlik (--print / -p) modda çalıştır",
    )
    parser.add_argument(
        "-t",
        "--tool",
        choices=["claude", "agy"],
        default=None,
        help="Aracı zorla (varsayılan: Jev otomatik seçer, sadece alt modeli belirler)",
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="Komutu başlatmadan yalnızca Jev kararını ve üretilen komutu göster",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Karar çıktısını JSON olarak yazdır (ve --dry-run gibi çık)",
    )
    parser.add_argument(
        "-y",
        "--yes",
        dest="skip_permissions",
        action="store_true",
        help="--dangerously-skip-permissions bayrağını ekle",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Hedef çalışma klasörü (varsayılan: mevcut dizin)",
    )
    parser.add_argument(
        "--decision-model",
        default=DEFAULT_DECISION_MODEL,
        help=f"Ollaya karar modeli (varsayılan: {DEFAULT_DECISION_MODEL})",
    )

    args, unknown = parser.parse_known_args()
    task_text = " ".join(args.task).strip()
    if not task_text and not sys.stdin.isatty():
        task_text = sys.stdin.read().strip()

    if not task_text:
        parser.print_help()
        return 2

    target_cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    import jrc_director
    # Hız: yönetici iş emrini jev kararını beklemeden, analizle paralel yazmaya başlar.
    prefetch = None if (args.json or args.dry_run) else jrc_director.start_prefetch(task_text, target_cwd)
    try:
        decision = analyze_task_with_jev(
            task=task_text,
            cwd=target_cwd,
            decision_model=args.decision_model,
            forced_tool=args.tool,
        )
    except BaseException:
        if prefetch:
            prefetch.cancel()
        raise
    prefetch = jrc_director.resolve_prefetch(prefetch, decision)
    cmd = build_cli_command(
        decision=decision,
        task=task_text,
        print_mode=args.print_mode,
        skip_permissions=args.skip_permissions,
        extra_args=unknown,
    )

    if args.json:
        mem_info = jrc_context.get_workspace_memory_summary(target_cwd)
        output = {**decision, "command": cmd, "cwd": str(target_cwd), "memory": mem_info}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    print_banner(decision, cmd, cwd=target_cwd)

    if args.dry_run:
        emir_ctx = jrc_context.build_emir_context_block(task_text)
        if emir_ctx:
            print(f"\n\033[2m{emir_ctx}\033[0m", file=sys.stderr)
        return 0

    # Görev öncesi ortak hafıza katmanını (.jrc_context.md) saf Python ile güncelle
    try:
        jrc_context.record_task_and_sync(target_cwd, task_text, decision)
        import jrc_gate
        jrc_gate.record("jroute", decision, task_text)
    except Exception as exc:
        sys.stderr.write(f"Hafıza güncellenirken uyarı: {exc}\n")

    if args.cwd:
        os.chdir(target_cwd)

    if jrc_director.active_for(decision):
        # jrc motoru/modeli seçti; işin nasıl yapılacağını ve hafıza notunu yönetici (Opus) yönetir.
        rc = jrc_director.run(
            task_text, decision, target_cwd, args.print_mode,
            lambda brief: build_cli_command(
                decision=decision, task=task_text, print_mode=args.print_mode,
                skip_permissions=args.skip_permissions, extra_args=unknown, brief=brief,
            ),
            prefetch=prefetch,
        )
        try:
            jrc_context.sync_workspace_memory(target_cwd)
        except Exception:
            pass
        return rc

    os.execvp(cmd[0], cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
