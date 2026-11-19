#!/home/efe/Desktop/jev/.venv/bin/python3
"""
Jev Router Etkileşimli Terminal Konsolu (jrc)

`router.py` (`jroute`) dosyasına dokunmadan onun analiz motorunu kullanan,
terminalde `jrc` yazıldığında doğrudan mesaj girilebilen etkileşimli CLI arayüzü.
"""

from __future__ import annotations

import argparse
import json
import os
import readline
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Aynı klasördeki router.py modülünü içe aktar
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jrc_context  # noqa: E402
import jrc_director  # noqa: E402
import jrc_gate  # noqa: E402
import jrc_usage  # noqa: E402
import router  # noqa: E402

CONSOLE_COMMANDS = [
    "/auto",
    "/claude",
    "/agy",
    "/tool",
    "/p",
    "/print",
    "/d",
    "/dry",
    "/y",
    "/yes",
    "/m",
    "/multi",
    "/cd",
    "/ctx",
    "/context",
    "/usage",
    "/status",
    "/models",
    "/clear",
    "/help",
    "/q",
    "/exit",
]


class Spinner:
    """Analiz sırasında terminalde animasyonlu durum göstergesi."""

    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, message: str = "Jev görev analizi yapılıyor...", enabled: bool = True) -> None:
        self.message = message
        self.enabled = enabled and sys.stderr.isatty()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _spin(self) -> None:
        t0 = time.perf_counter()
        idx = 0
        while not self._stop_event.is_set():
            elapsed = time.perf_counter() - t0
            frame = self.FRAMES[idx % len(self.FRAMES)]
            sys.stderr.write(
                f"\r\033[K\033[36m{frame}\033[0m \033[1m{self.message}\033[0m "
                f"\033[2m({elapsed:.1f}s)\033[0m"
            )
            sys.stderr.flush()
            idx += 1
            self._stop_event.wait(0.08)

    def __enter__(self) -> Spinner:
        if self.enabled:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.enabled and self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=0.5)
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def print_console_banner(decision: dict[str, Any], cmd: list[str], cwd: Path | None = None) -> None:
    """Seçim özetini olasılık dağılımıyla birlikte terminale yazdırır."""
    bold = "\033[1m"
    cyan = "\033[36m"
    green = "\033[32m"
    yellow = "\033[33m"
    magenta = "\033[35m"
    dim = "\033[2m"
    reset = "\033[0m"

    target_dir = cwd or Path.cwd()
    mem = jrc_context.get_workspace_memory_summary(target_dir)
    mem_tag = f" · {mem['task_count']} görev kayıtlı" if mem["has_memory"] else " · yeni başlatıldı"

    m = decision["metrics"]
    tool_badge = f"{green}claude{reset}" if decision["tool"] == "claude" else f"{cyan}agy{reset}"
    top_routes = sorted(m["route_probabilities"].items(), key=lambda x: x[1], reverse=True)[:3]
    probs_str = " · ".join(f"{k}:{int(round(v * 100))}%" for k, v in top_routes)

    print(
        f"{bold}⚡ Jev Router{reset} {dim}[{decision['decision_model']} · {decision['latency_ms']} ms]{reset}\n"
        f"  ├─ Araç & Model : {tool_badge} → {bold}{decision['model']}{reset} ({decision['label']})\n"
        f"  ├─ Efor & Risk  : effort={yellow}{decision['effort']}{reset} | "
        f"karmaşıklık={m['complexity_level']} ({m['complexity_score']}) | "
        f"kritiklik={m['critical_probability']}\n"
        f"  ├─ Olasılıklar  : {magenta}{probs_str}{reset}\n"
        f"  ├─ Ortak Hafıza : {green}.jrc_context.md{reset} {dim}[Git-free{mem_tag}]{reset}\n"
        f"  └─ Komut        : {dim}{' '.join(
            [repr(a[:60] + ('…' if len(a) > 60 else '')) if ' ' in a else a for a in cmd]
        )}{reset}",
        file=sys.stderr,
    )


def _setup_readline() -> Path:
    """Readline geçmişini ve sekme (Tab) tamamlamasını yapılandırır."""
    history_dir = Path.home() / ".cache" / "jroute"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_file = history_dir / "history"
    try:
        if history_file.exists():
            readline.read_history_file(str(history_file))
        readline.set_history_length(500)
    except Exception:
        pass

    def completer(text: str, state: int) -> str | None:
        buf = readline.get_line_buffer().lstrip()
        if buf.startswith("/cd "):
            prefix = buf[4:].strip()
            base = Path(prefix).expanduser() if prefix else Path.cwd()
            parent = base if (prefix.endswith("/") or not prefix) else base.parent
            part = "" if (prefix.endswith("/") or not prefix) else base.name
            try:
                matches = [
                    str(p.name) + "/"
                    for p in sorted(parent.iterdir())
                    if p.is_dir() and p.name.startswith(part)
                ]
            except Exception:
                matches = []
            return matches[state] if state < len(matches) else None

        options = [c for c in CONSOLE_COMMANDS if c.startswith(text)]
        return options[state] if state < len(options) else None

    try:
        readline.set_completer_delims(" \t\n;")
        readline.set_completer(completer)
        readline.parse_and_bind("tab: complete")
    except Exception:
        pass

    return history_file


def _save_history(history_file: Path) -> None:
    try:
        readline.write_history_file(str(history_file))
    except Exception:
        pass


def _print_console_header(
    cwd: Path,
    forced_tool: str | None,
    print_mode: bool,
    dry_run: bool,
    skip_permissions: bool,
    decision_model: str,
) -> None:
    bold = "\033[1m"
    cyan = "\033[36m"
    green = "\033[32m"
    yellow = "\033[33m"
    magenta = "\033[35m"
    dim = "\033[2m"
    reset = "\033[0m"

    ws = router.collect_workspace_context(cwd)
    stack = ", ".join(ws["stack_markers"]) if ws["stack_markers"] else "genel"
    mem = jrc_context.get_workspace_memory_summary(cwd)
    mem_tag = f"{mem['task_count']} görev · {mem['modified_count']} son değişiklik" if mem["has_memory"] else "başlatılıyor"
    tool_str = (
        f"{green}claude{reset}"
        if forced_tool == "claude"
        else (f"{cyan}agy{reset}" if forced_tool == "agy" else f"{magenta}otomatik (claude / agy){reset}")
    )
    modes = [
        f"{yellow}dry-run{reset}"
        if dry_run
        else (f"{cyan}print (-p){reset}" if print_mode else f"{green}interaktif{reset}")
    ]
    if skip_permissions:
        modes.append(f"{yellow}skip-perms (-y){reset}")

    print(
        f"{cyan}╭──────────────────────────────────────────────────────────────────────╮{reset}\n"
        f"{cyan}│{reset} {bold}⚡ Jev Router Konsolu (jrc){reset} {dim}(Ollaya: {decision_model}){reset}\n"
        f"{cyan}│{reset} 📂 {bold}Dizin  :{reset} {cwd} {dim}[{stack}]{reset}\n"
        f"{cyan}│{reset} 🎯 {bold}Hedef  :{reset} {tool_str}   ⚙️  {bold}Mod :{reset} {' · '.join(modes)}\n"
        f"{cyan}│{reset} 🧠 {bold}Hafıza :{reset} {green}.jrc_context.md{reset} {dim}[{mem_tag}]{reset}\n"
        f"{cyan}│{reset} 💡 {dim}Komut  : /claude · /agy · /auto · /ctx · /usage · /d · /p · /y · /m · /help · q / Ctrl+C{reset}\n"
        f"{cyan}╰──────────────────────────────────────────────────────────────────────╯{reset}"
    )


def _print_console_help() -> None:
    bold = "\033[1m"
    cyan = "\033[36m"
    dim = "\033[2m"
    reset = "\033[0m"
    print(
        f"\n{bold}Kullanılabilir Konsol Komutları ve Kısayollar:{reset}\n"
        f"  {cyan}<mesaj>{reset}          Mesajı Jev ile analiz eder ve seçilen aracı (claude/agy) başlatır\n"
        f"  {cyan}!d <mesaj>{reset}       Sadece bu mesaj için karar analizini gösterir (komutu başlatmaz)\n"
        f"  {cyan}!p <mesaj>{reset}       Sadece bu mesajı tek seferlik (--print) modda çalıştırıp konsola döner\n"
        f"  {cyan}@claude <mesaj>{reset}  Sadece bu mesaj için Claude Code seçmeye zorlar\n"
        f"  {cyan}@agy <mesaj>{reset}     Sadece bu mesaj için Antigravity (agy) seçmeye zorlar\n"
        f"  {cyan}/claude | /agy | /auto{reset} Varsayılan hedef aracı değiştirir\n"
        f"  {cyan}/ctx | /context{reset}        Ortak hafıza (.jrc_context.md) özetini gösterir (/ctx sync, /ctx note ...)\n"
        f"  {cyan}/d | /dry{reset}              Dry-run (sadece karar testi) modunu aç/kapat\n"
        f"  {cyan}/p | /print{reset}      Print (-p) modunu aç/kapat\n"
        f"  {cyan}/y | /yes{reset}        İzin atlama (--dangerously-skip-permissions) modunu aç/kapat\n"
        f"  {cyan}/m | /multi{reset}      Çok satırlı mesaj giriş modunu başlatır (bitirmek için boş satırda . veya END)\n"
        f"  {cyan}/cd <dizin>{reset}      Çalışma dizinini değiştirir\n"
        f"  {cyan}/models{reset}          Desteklenen tüm araç ve alt model profillerini listeler\n"
        f"  {cyan}/usage{reset}           Bağlı ürünlerin (agy, claude, ollaya) kullanım ve kota durumunu gösterir\n"
        f"  {cyan}/status{reset}          Aktif dizin ve mod durumunu gösterir\n"
        f"  {cyan}/clear{reset}           Ekranı temizler\n"
        f"  {cyan}q | exit | Ctrl+C | Ctrl+D{reset} Konsoldan çıkar\n"
        f"  {dim}İpucu: Satır sonuna '\\' koyarak da alt satıra devam edebilirsiniz.{reset}\n"
    )


def _read_multiline_input() -> str:
    """Çok satırlı görev metni okur."""
    print(
        "\033[2mÇok satırlı giriş modu (Göndermek için tek başına '.' veya 'END' yazıp Enter'a basın, iptal için Ctrl+C):\033[0m"
    )
    lines: list[str] = []
    idx = 1
    while True:
        try:
            line = input(f"\001\033[2m\002  {idx:2d} │ \001\033[0m\002")
        except EOFError:
            break
        if line.strip() in (".", "END", "/end"):
            break
        lines.append(line)
        idx += 1
    return "\n".join(lines).strip()


def run_interactive_console(args: argparse.Namespace, extra_args: list[str]) -> int:
    history_file = _setup_readline()
    target_cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    forced_tool: str | None = args.tool
    print_mode: bool = args.print_mode
    dry_run: bool = args.dry_run
    skip_permissions: bool = args.skip_permissions
    decision_model: str = args.decision_model

    _print_console_header(
        cwd=target_cwd,
        forced_tool=forced_tool,
        print_mode=print_mode,
        dry_run=dry_run,
        skip_permissions=skip_permissions,
        decision_model=decision_model,
    )

    while True:
        tool_tag = forced_tool if forced_tool else "auto"
        flags_tag = ""
        if dry_run:
            flags_tag += "·dry"
        elif print_mode:
            flags_tag += "·print"
        if skip_permissions:
            flags_tag += "·yes"

        badge_color = (
            "\033[32m" if forced_tool == "claude" else ("\033[36m" if forced_tool == "agy" else "\033[35m")
        )
        prompt = (
            f"\001\033[1m\002jrc\001\033[0m\002 "
            f"\001{badge_color}\002[{tool_tag}{flags_tag}]\001\033[0m\002 "
            f"\001\033[1;36m\002❯\001\033[0m\002 "
        )

        try:
            raw = input(prompt)
        except (KeyboardInterrupt, EOFError):
            print()
            break

        cancelled = False
        while raw.endswith("\\"):
            raw = raw[:-1] + "\n"
            try:
                cont = input("\001\033[2m\002  … ❯ \001\033[0m\002")
                raw += cont
            except KeyboardInterrupt:
                print("\n\033[2mGiriş iptal edildi.\033[0m")
                cancelled = True
                break
            except EOFError:
                print()
                cancelled = True
                break

        if cancelled:
            continue

        text = raw.strip()
        if not text:
            continue

        _save_history(history_file)

        if text.lower() in ("q", "quit", "exit", "/q", "/quit", "/exit"):
            break

        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd_name = parts[0].lower()
            cmd_arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd_name in ("/help", "/h", "/?"):
                _print_console_help()
                continue
            if cmd_name == "/clear":
                os.system("clear")
                _print_console_header(
                    target_cwd, forced_tool, print_mode, dry_run, skip_permissions, decision_model
                )
                continue
            if cmd_name == "/status":
                _print_console_header(
                    target_cwd, forced_tool, print_mode, dry_run, skip_permissions, decision_model
                )
                continue
            if cmd_name in ("/usage", "/u"):
                jrc_usage.print_usage_report(target_cwd, as_json=getattr(args, "json", False))
                continue
            if cmd_name == "/models":
                print("\n\033[1mDesteklenen Jev Route Profilleri:\033[0m")
                for key, prof in router.ROUTE_PROFILES.items():
                    t_col = "\033[32m" if prof["tool"] == "claude" else "\033[36m"
                    print(
                        f"  • \033[1m{key:<14}\033[0m → {t_col}{prof['tool']:<6}\033[0m | "
                        f"\033[1m{prof['model']:<25}\033[0m | effort={prof['default_effort']}\n"
                        f"    \033[2m{prof['criteria']}\033[0m"
                    )
                print()
                continue
            if cmd_name == "/claude":
                if cmd_arg:
                    text = f"@claude {cmd_arg}"
                else:
                    forced_tool = "claude"
                    print("\033[32m✓ Hedef araç 'claude' olarak sabitlendi.\033[0m")
                    continue
            elif cmd_name == "/agy":
                if cmd_arg:
                    text = f"@agy {cmd_arg}"
                else:
                    forced_tool = "agy"
                    print("\033[36m✓ Hedef araç 'agy' olarak sabitlendi.\033[0m")
                    continue
            elif cmd_name == "/auto":
                forced_tool = None
                print("\033[35m✓ Hedef araç 'otomatik' (Jev seçimi) olarak ayarlandı.\033[0m")
                continue
            elif cmd_name == "/tool":
                val = cmd_arg.lower()
                if val in ("claude", "agy"):
                    forced_tool = val
                    print(f"✓ Hedef araç '{val}' olarak ayarlandı.")
                elif val in ("auto", "otomatik", ""):
                    forced_tool = None
                    print("✓ Hedef araç 'otomatik' olarak ayarlandı.")
                else:
                    print("\033[33mKullanım: /tool <auto|claude|agy>\033[0m")
                continue
            elif cmd_name in ("/d", "/dry"):
                if cmd_arg:
                    text = f"!d {cmd_arg}"
                else:
                    dry_run = not dry_run
                    print(f"✓ Dry-run (sadece karar testi) modu: {'AÇIK' if dry_run else 'KAPALI'}")
                    continue
            elif cmd_name in ("/p", "/print"):
                if cmd_arg:
                    text = f"!p {cmd_arg}"
                else:
                    print_mode = not print_mode
                    print(f"✓ Print (-p) modu: {'AÇIK' if print_mode else 'KAPALI'}")
                    continue
            elif cmd_name in ("/y", "/yes"):
                skip_permissions = not skip_permissions
                print(f"✓ İzin atlama (-y) modu: {'AÇIK' if skip_permissions else 'KAPALI'}")
                continue
            elif cmd_name in ("/m", "/multi"):
                try:
                    text = _read_multiline_input()
                except KeyboardInterrupt:
                    print("\n\033[2mÇok satırlı giriş iptal edildi.\033[0m")
                    continue
                if not text:
                    continue
            elif cmd_name in ("/ctx", "/context"):
                ctx_args = cmd_arg.split(maxsplit=1)
                sub_cmd = ctx_args[0].lower() if ctx_args else ""
                sub_val = ctx_args[1].strip() if len(ctx_args) > 1 else ""

                if sub_cmd in ("note", "not"):
                    if not sub_val:
                        print("\033[33mKullanım: /ctx note <kalıcı not metni>\033[0m")
                    else:
                        ok = jrc_context.append_user_note(target_cwd, sub_val)
                        if ok:
                            print(f"\033[32m✓ Not .jrc_context.md dosyasına eklendi:\033[0m {sub_val}")
                        else:
                            print("\033[31m✗ Not eklenirken hata oluştu.\033[0m")
                    continue
                elif sub_cmd in ("sync", "refresh", "tara"):
                    state, ctx_file = jrc_context.sync_workspace_memory(target_cwd)
                    changes = state.get("recent_changes", {})
                    mod_c = len(changes.get("modified", []))
                    add_c = len(changes.get("added", []))
                    del_c = len(changes.get("deleted", []))
                    print(
                        f"\033[32m✓ .jrc_context.md güncellendi:\033[0m "
                        f"{len(state.get('file_snapshot', {}))} dosya tarandı | "
                        f"+{add_c} yeni · ~{mod_c} değişen · -{del_c} silinen"
                    )
                    continue
                elif sub_cmd in ("cat", "show", "goster", "full"):
                    ctx_file = target_cwd / jrc_context.CONTEXT_FILENAME
                    if ctx_file.exists():
                        print(f"\n\033[1m--- {ctx_file} ---\033[0m\n")
                        print(ctx_file.read_text(encoding="utf-8"))
                        print("\033[1m----------------------------------------\033[0m\n")
                    else:
                        print(f"\033[33mHenüz {jrc_context.CONTEXT_FILENAME} oluşturulmadı. /ctx sync ile oluşturabilirsiniz.\033[0m")
                    continue
                else:
                    mem = jrc_context.get_workspace_memory_summary(target_cwd)
                    ctx_file = target_cwd / jrc_context.CONTEXT_FILENAME
                    print(f"\n\033[1;36m🧠 JRC Ortak Hafıza Özeti (.jrc_context.md):\033[0m")
                    print(f"  ├─ Konum        : {ctx_file} {'(\033[32mmevcut\033[0m)' if ctx_file.exists() else '(\033[33mhenüz yok\033[0m)'}")
                    print(f"  ├─ Toplam Görev : {mem['task_count']} adet kayıtlı")
                    if mem["recent_tasks"]:
                        print(f"  ├─ Son Görevler :")
                        for idx, t in enumerate(mem["recent_tasks"][:3], 1):
                            t_short = (t[:70] + "…") if len(t) > 70 else t
                            print(f"  │    {idx}. \033[2m{t_short}\033[0m")
                    if mem["recent_modified_files"]:
                        print(f"  ├─ Değişenler   : {', '.join(mem['recent_modified_files'][:5])}")
                    user_notes = ""
                    if ctx_file.exists():
                        try:
                            user_notes = jrc_context.extract_user_notes(ctx_file.read_text(encoding="utf-8"))
                        except Exception:
                            user_notes = ""
                    if user_notes:
                        notes_disp = (user_notes[:100] + "…") if len(user_notes) > 100 else user_notes
                        print(f"  └─ Kalıcı Notlar: \033[33m{notes_disp}\033[0m")
                    else:
                        print(f"  └─ Kalıcı Notlar: \033[2m(Henüz not eklenmedi. /ctx note <metin> ile ekleyebilirsiniz)\033[0m")
                    print(f"\033[2mKomutlar: /ctx sync (yeniden tara) · /ctx note <metin> · /ctx cat (tamamı)\033[0m\n")
                    continue
            elif cmd_name == "/cd":
                if not cmd_arg:
                    print(f"Mevcut dizin: {target_cwd}")
                    continue
                new_dir = Path(cmd_arg).expanduser()
                if not new_dir.is_absolute():
                    new_dir = (target_cwd / new_dir).resolve()
                else:
                    new_dir = new_dir.resolve()
                if new_dir.is_dir():
                    target_cwd = new_dir
                    os.chdir(target_cwd)
                    try:
                        jrc_context.sync_workspace_memory(target_cwd)
                    except Exception:
                        pass
                    ws = router.collect_workspace_context(target_cwd)
                    stack = ", ".join(ws["stack_markers"]) if ws["stack_markers"] else "genel"
                    print(
                        f"\033[32m✓ Çalışma dizini değiştirildi:\033[0m {target_cwd} \033[2m[{stack} · hafıza aktif]\033[0m"
                    )
                else:
                    print(f"\033[31m✗ Dizin bulunamadı:\033[0m {new_dir}")
                continue
            else:
                print(f"\033[33mBilinmeyen komut: {cmd_name} (Yardım için /help yazın)\033[0m")
                continue

        one_shot_dry = dry_run
        one_shot_print = print_mode
        one_shot_tool = forced_tool

        while True:
            if text.startswith("!d ") or text.startswith("!dry "):
                one_shot_dry = True
                text = text.split(maxsplit=1)[1].strip()
            elif text.startswith("!p ") or text.startswith("!print "):
                one_shot_print = True
                text = text.split(maxsplit=1)[1].strip()
            elif text.lower().startswith("@claude "):
                one_shot_tool = "claude"
                text = text.split(maxsplit=1)[1].strip()
            elif text.lower().startswith("@agy "):
                one_shot_tool = "agy"
                text = text.split(maxsplit=1)[1].strip()
            else:
                break

        if not text:
            continue

        # Hız: yönetici (Opus) iş emrini jev kararını beklemeden, analizle paralel yazmaya başlar.
        prefetch = None if (args.json or one_shot_dry) else jrc_director.start_prefetch(text, target_cwd)
        try:
            with Spinner("Jev görev analizi yapılıyor...", enabled=not args.json):
                decision = router.analyze_task_with_jev(
                    task=text,
                    cwd=target_cwd,
                    decision_model=decision_model,
                    forced_tool=one_shot_tool,
                )
        except KeyboardInterrupt:
            if prefetch:
                prefetch.cancel()
            print("\n\033[2mİşlem iptal edildi.\033[0m\n")
            continue
        except Exception as exc:
            if prefetch:
                prefetch.cancel()
            print(f"\033[31m✗ Jev analiz hatası:\033[0m {exc}", file=sys.stderr)
            continue
        prefetch = jrc_director.resolve_prefetch(prefetch, decision)

        cmd = router.build_cli_command(
            decision=decision,
            task=text,
            print_mode=one_shot_print,
            skip_permissions=skip_permissions,
            extra_args=extra_args,
        )

        if args.json:
            mem_info = jrc_context.get_workspace_memory_summary(target_cwd)
            output = {**decision, "command": cmd, "cwd": str(target_cwd), "memory": mem_info}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            if one_shot_dry:
                continue
            return 0

        print_console_banner(decision, cmd, cwd=target_cwd)

        if one_shot_dry:
            emir_ctx = jrc_context.build_emir_context_block(text)
            if emir_ctx:
                print(f"\n\033[2m{emir_ctx}\033[0m", file=sys.stderr)
            print()
            continue

        # Görevi çalıştırmadan önce .jrc_context.md ortak hafıza katmanına kaydet
        try:
            jrc_context.record_task_and_sync(target_cwd, text, decision)
            jrc_gate.record("jrc", decision, text)
        except Exception:
            pass

        os.chdir(target_cwd)

        # jrc motoru/modeli seçti; işin nasıl yapılacağını ve hafıza notunu yönetici (Opus) yönetir.
        def run_agent(_text: str = text, _decision: dict[str, Any] = decision,
                      _print: bool = one_shot_print, _cmd: list[str] = cmd,
                      _prefetch: Any = prefetch) -> None:
            if not jrc_director.active_for(_decision):
                subprocess.run(_cmd, cwd=str(target_cwd))
                return
            jrc_director.run(
                _text, _decision, target_cwd, _print,
                lambda brief: router.build_cli_command(
                    decision=_decision, task=_text, print_mode=_print,
                    skip_permissions=skip_permissions, extra_args=extra_args, brief=brief,
                ),
                prefetch=_prefetch,
            )

        if one_shot_print:
            print()
            try:
                run_agent()
            except KeyboardInterrupt:
                print()
            print()
            _refresh_tokens_bg()
            try:
                jrc_context.sync_workspace_memory(target_cwd)
            except Exception:
                pass
            continue

        # İnteraktif oturumu çalıştır ve çıkışta jrc konsoluna dönerek hafızayı senkronize et
        old_tc = None
        try:
            if sys.stdin.isatty():
                import termios
                old_tc = termios.tcgetattr(sys.stdin)
        except Exception:
            pass

        try:
            run_agent()
        except KeyboardInterrupt:
            print()
        finally:
            if old_tc is not None and sys.stdin.isatty():
                try:
                    import termios
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tc)
                except Exception:
                    pass

        _refresh_tokens_bg()
        try:
            jrc_context.sync_workspace_memory(target_cwd)
            print("\n\033[32m✓ Ajan oturumu tamamlandı. .jrc_context.md hafızası güncellendi.\033[0m\n")
        except Exception:
            pass

    _save_history(history_file)
    return 0


def _refresh_tokens_bg() -> None:
    """Görev bitince token defterini (jrc_tokens) arka planda günceller; panel beklemeden güncel kalır."""
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parent / "jrc_tokens.py"), "--sessiz"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception:
        pass


def run_gate_command(name: str, rest: list[str]) -> int:
    """`jrc gate|audit|engine|director|policy` — bot/servis çağrılarının jrc geçidi kontrolü."""
    bold, dim, green, yellow, red, reset = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
    if name == "engine":
        if rest:
            eng = jrc_gate.set_engine(rest[0].lower())
            print(f"{green}✓ Motor kilidi: {eng}{reset}")
        else:
            print(f"Motor kilidi: {jrc_gate.load_policy().get('engine', 'auto')}  (kullanım: jrc engine auto|claude|agy)")
        return 0
    if name == "fable":
        if rest and rest[0].lower() in ("on", "off", "aç", "ac", "kapat"):
            on = rest[0].lower() in ("on", "aç", "ac")
            jrc_gate.set_route_substitute({} if on else {"claude_fable": "claude_opus"})
        sub = jrc_gate.load_policy().get("route_substitute") or {}
        durum = f"{red}kapalı → {sub['claude_fable']}{reset}" if sub.get("claude_fable") else f"{green}açık{reset}"
        print(f"Fable: {durum}  {dim}(kullanım: jrc fable on|off · off = kota ikamesi fable→opus){reset}")
        return 0
    if name == "director":
        if rest and rest[0].lower() in ("on", "off", "aç", "ac", "kapat"):
            jrc_director.set_enabled(rest[0].lower() in ("on", "aç", "ac"))
        cfg = jrc_director.config()
        durum = f"{green}açık{reset}" if cfg.get("enabled") else f"{red}kapalı{reset}"
        print(f"Yönetici: {durum} · model {cfg['model']} · efor {cfg['effort']} · zaman aşımı {cfg['timeout_s']} sn"
              f"  {dim}(kullanım: jrc director on|off){reset}\n"
              f"  {dim}basit görevi atla: {cfg.get('skip_trivial')} · oturum devamı: {cfg.get('reuse_session')}{reset}")
        return 0
    if name == "policy":
        print(json.dumps(jrc_gate.load_policy(), ensure_ascii=False, indent=2))
        print(f"{dim}Düzenle: {jrc_gate.POLICY_FILE}{reset}")
        return 0
    if name == "audit":
        rows = jrc_gate.audit()
        bad = [r for r in rows if not r["gated"]]
        for r in rows:
            mark = f"{green}geçitli{reset}" if r["gated"] else f"{red}DOĞRUDAN{reset}"
            print(f"  {mark}  {r['file']}  {dim}satır {r['lines']}{reset}")
        print(f"\n{bold}{len(rows)} dosya claude/agy referansı içeriyor, {len(bad)} tanesi geçide bağlı değil.{reset}")
        return 1 if bad else 0
    hours = float(rest[0]) if rest else 24.0
    sm = jrc_gate.ledger_summary(hours)
    pol = jrc_gate.load_policy()
    print(f"\n{bold}⚡ JRC Geçidi{reset}  motor kilidi: {yellow}{pol.get('engine', 'auto')}{reset}  "
          f"{dim}(son {hours:g} saat · {sm['decisions']} karar · {sm['fallbacks']} fallback){reset}")
    for src, d in sorted(sm["by_source"].items()):
        pin = pol["sources"].get(src, {}).get("pin")
        dist = " · ".join(f"{k} ×{v}" for k, v in sorted(d.items(), key=lambda x: -x[1]))
        print(f"  ├─ {bold}{src:<14}{reset} {dim}{'pin:' + pin if pin else 'analiz'}{reset}  {dist}")
    if not sm["by_source"]:
        print(f"  └─ {dim}henüz kayıt yok ({jrc_gate.LEDGER_FILE}){reset}")
    print(f"{dim}Komutlar: jrc gate [saat] · jrc audit · jrc engine auto|claude|agy · jrc fable on|off · jrc director on|off · jrc policy{reset}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="jrc",
        description="Jev Router Etkileşimli Terminal Konsolu",
    )
    parser.add_argument(
        "-p",
        "--print",
        dest="print_mode",
        action="store_true",
        help="Varsayılan olarak tek seferlik (--print / -p) modda başlat",
    )
    parser.add_argument(
        "-t",
        "--tool",
        choices=["claude", "agy"],
        default=None,
        help="Hedef aracı zorla (claude veya agy)",
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="Varsayılan olarak dry-run (sadece karar testi) modunda başlat",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Karar çıktısını JSON olarak yazdır",
    )
    parser.add_argument(
        "-y",
        "--yes",
        dest="skip_permissions",
        action="store_true",
        default=True,
        help="--dangerously-skip-permissions bayrağını ekle (varsayılan: açık)",
    )
    parser.add_argument(
        "--no-yes",
        "--no-y",
        dest="skip_permissions",
        action="store_false",
        help="İzin atlama modunu devre dışı bırak",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Hedef çalışma klasörü (varsayılan: mevcut dizin)",
    )
    parser.add_argument(
        "--decision-model",
        default=router.DEFAULT_DECISION_MODEL,
        help=f"Ollaya karar modeli (varsayılan: {router.DEFAULT_DECISION_MODEL})",
    )

    args, unknown = parser.parse_known_args()
    if unknown and unknown[0].lower() == "web":
        import jrc_web
        return jrc_web.main(unknown[1:])
    if unknown and unknown[0].lower() in ("gate", "audit", "engine", "fable", "director", "policy"):
        return run_gate_command(unknown[0].lower(), unknown[1:])
    if unknown and unknown[0].lower() in ("tokens", "token"):
        import jrc_tokens
        return jrc_tokens.main(unknown[1:] + (["--json"] if args.json else []))
    if unknown and unknown[0].lower() == "usage":
        target_cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
        jrc_usage.print_usage_report(target_cwd, as_json=args.json)
        return 0
    return run_interactive_console(args, unknown)


if __name__ == "__main__":
    raise SystemExit(main())
