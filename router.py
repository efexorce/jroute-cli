#!/home/efe/Desktop/projeler/jev/.venv/bin/python3
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

from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

DEFAULT_BASE_URL = os.environ.get("TYPESAFE_BASE_URL", "http://127.0.0.1:11435")
DEFAULT_API_KEY = os.environ.get("TYPESAFE_API_KEY", "local")
DEFAULT_DECISION_MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "largha")

# Route profilleri: Tek adımda (araç + alt model + varsayılan efor)
ROUTE_PROFILES: dict[str, dict[str, str]] = {
    "agy_flash": {
        "tool": "agy",
        "model": "gemini-3.8-flash-medium",
        "default_effort": "low",
        "label": "Antigravity · Gemini 3.8 Flash",
        "criteria": (
            "Quick lookups, status checks, log inspection, simple shell commands, "
            "or trivial single-line questions that need fast execution."
        ),
    },
    "agy_argon": {
        "tool": "agy",
        "model": "gemini-4-argon-medium",
        "default_effort": "medium",
        "label": "Antigravity · Gemini 4 Argon",
        "criteria": (
            "Full-stack web development, UI/frontend design, API/webhook integration, "
            "automation bots (Telegram/WhatsApp), deployment, or multi-step workspace tasks."
        ),
    },
    "agy_hydrogen": {
        "tool": "agy",
        "model": "gemini-4-hydrogen-high",
        "default_effort": "high",
        "label": "Antigravity · Gemini 4 Hydrogen",
        "criteria": (
            "Deep research, multi-agent orchestration, complex algorithmic design, "
            "or system-wide investigation requiring maximum reasoning depth."
        ),
    },
    "claude_haiku": {
        "tool": "claude",
        "model": "claude-haiku-5-5",
        "default_effort": "low",
        "label": "Claude Code · Haiku 5.5",
        "criteria": (
            "Fast git operations (commit, push, branch), small script tweaks, "
            "formatting, README updates, or lightweight file edits."
        ),
    },
    "claude_sonnet": {
        "tool": "claude",
        "model": "claude-sonnet-5-5",
        "default_effort": "medium",
        "label": "Claude Code · Sonnet 5.5",
        "criteria": (
            "Standard software engineering, bug fixing, feature implementation, "
            "code refactoring, writing unit tests, and clean code edits."
        ),
    },
    "claude_opus": {
        "tool": "claude",
        "model": "claude-opus-5-5",
        "default_effort": "high",
        "label": "Claude Code · Opus 5.5",
        "criteria": (
            "Complex multi-file refactoring, critical production bug debugging, "
            "database schema/ORM architecture (Odoo, PostgreSQL), and high-risk backend code."
        ),
    },
}


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
    """Çalışma klasöründeki proje tipini ve dosyaları özetler."""
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
        (".git", "git-repo"),
    ]:
        if (cwd / name).exists():
            markers.append(label)

    return {
        "cwd": str(cwd),
        "project_name": cwd.name,
        "stack_markers": markers,
        "top_files": entries,
    }


def detect_explicit_tool_override(task: str, forced_tool: str | None) -> str | None:
    """Kullanıcı görev içinde açıkça 'claude ile' veya 'agy ile' dediyse algılar."""
    if forced_tool in ("claude", "agy"):
        return forced_tool
    lower = task.lower()
    if re.search(r"\b(claude\s+ile|use\s+claude|claude\s+code\s+ile)\b", lower):
        return "claude"
    if re.search(r"\b(agy\s+ile|antigravity\s+ile|use\s+agy|gemini\s+ile)\b", lower):
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
    ensure_ollaya_running(base_url)
    ws = collect_workspace_context(cwd)

    state = {
        "task": task,
        "workspace": ws,
    }

    route_criteria = {k: v["criteria"] for k, v in ROUTE_PROFILES.items()}

    t0 = time.perf_counter()
    with TypeSafeClient(base_url=base_url, api_key=api_key) as client:
        response = client.system_one(
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
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

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

    # Kullanıcı açıkça araç belirttiyse veya tool sorusu yüksek güvenle farklı araç seçtiyse
    explicit_tool = detect_explicit_tool_override(task, forced_tool)
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

    # Çok basit görevlerde (ör. commit push, durum kontrolü) hafif alt modeli tercih et
    lower_task = task.lower().strip()
    is_quick_git = bool(
        re.fullmatch(r"(git\s+)?(commit|push|status|pull|diff)(\s+et)?(\s+push)?", lower_task)
    )
    if is_quick_git:
        effort = "low"
        chosen_route_key = "claude_haiku" if target_tool == "claude" else "agy_flash"
    elif effort == "xhigh" or (effort == "high" and crit_prob >= 0.70):
        # Çok yüksek karmaşıklıkta ilgili aracın en güçlü modeline yükselt
        if target_tool == "claude" and chosen_route_key == "claude_haiku":
            chosen_route_key = "claude_opus"
        elif target_tool == "agy" and chosen_route_key == "agy_flash":
            chosen_route_key = "agy_argon"

    profile = ROUTE_PROFILES[chosen_route_key]
    final_tool = profile["tool"]
    final_model = profile["model"]

    # agy model isimlerinde efor soneki uyumu (gemini-4-argon-low/medium/high vb.)
    if final_tool == "agy":
        agy_effort_suffix = "high" if effort in ("high", "xhigh") else ("low" if effort == "low" else "medium")
        if final_model.startswith("gemini-4-argon"):
            final_model = f"gemini-4-argon-{agy_effort_suffix}"
        elif final_model.startswith("gemini-3.8-flash"):
            final_model = f"gemini-3.8-flash-{agy_effort_suffix}"
        elif final_model.startswith("gemini-4-hydrogen"):
            final_model = "gemini-4-hydrogen-xhigh" if effort == "xhigh" else f"gemini-4-hydrogen-{agy_effort_suffix}"

    return {
        "tool": final_tool,
        "model": final_model,
        "effort": effort,
        "route_key": chosen_route_key,
        "label": profile["label"],
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
) -> list[str]:
    """Seçilen araç ve alt modele göre çalıştırılacak CLI komut dizisini üretir."""
    tool = decision["tool"]
    model = decision["model"]
    effort = decision["effort"]
    extra = extra_args or []

    if tool == "claude":
        cmd = ["claude", "--model", model, "--effort", effort]
        if skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if print_mode:
            cmd.append("-p")
        cmd.extend(extra)
        cmd.append(task)
        return cmd

    # tool == "agy"
    cmd = ["agy", "--model", model, "--effort", effort]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if print_mode:
        cmd.extend(["-p", task])
    else:
        cmd.extend(["-i", task])
    cmd.extend(extra)
    return cmd


def print_banner(decision: dict[str, Any], cmd: list[str]) -> None:
    """Seçim özetini terminale renkli ve okunaklı biçimde yazdırır."""
    bold = "\033[1m"
    cyan = "\033[36m"
    green = "\033[32m"
    yellow = "\033[33m"
    dim = "\033[2m"
    reset = "\033[0m"

    m = decision["metrics"]
    tool_badge = f"{green}claude{reset}" if decision["tool"] == "claude" else f"{cyan}agy{reset}"
    print(
        f"{bold}⚡ Jev Router{reset} {dim}[{decision['decision_model']} · {decision['latency_ms']} ms]{reset}\n"
        f"  ├─ Araç & Model : {tool_badge} → {bold}{decision['model']}{reset} ({decision['label']})\n"
        f"  ├─ Efor & Risk  : effort={yellow}{decision['effort']}{reset} | "
        f"karmaşıklık={m['complexity_level']} ({m['complexity_score']}) | "
        f"kritiklik={m['critical_probability']}\n"
        f"  └─ Komut        : {dim}{' '.join(
            [repr(a) if ' ' in a else a for a in cmd[:-1]] + [repr(cmd[-1][:60] + ('…' if len(cmd[-1]) > 60 else ''))]
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
    decision = analyze_task_with_jev(
        task=task_text,
        cwd=target_cwd,
        decision_model=args.decision_model,
        forced_tool=args.tool,
    )
    cmd = build_cli_command(
        decision=decision,
        task=task_text,
        print_mode=args.print_mode,
        skip_permissions=args.skip_permissions,
        extra_args=unknown,
    )

    if args.json:
        output = {**decision, "command": cmd, "cwd": str(target_cwd)}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    print_banner(decision, cmd)

    if args.dry_run:
        return 0

    if args.cwd:
        os.chdir(target_cwd)

    os.execvp(cmd[0], cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
