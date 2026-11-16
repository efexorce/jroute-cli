"""JRC Yönetici Katmanı (jrc_director) — işin NASIL yapılacağını Fable yönetir.

Akış (jrc / jroute insan görevleri):
    1. jrc motoru + modeli + eforu seçer (router.analyze_task_with_jev) — değişmedi.
    2. Fable salt-okunur araçlarla projeye bakıp yürütücü için bir "iş emri" yazar.
    3. Seçilen yürütücü (claude/agy) iş emriyle çalışır; `.jrc_context.md`'ye YAZMAZ,
       bitince kısa raporunu ~/.cache/jroute/reports/ altına bırakır.
    4. Fable raporu + değişen dosyaları denetler ve `.jrc_context.md` içindeki
       'Ortak Notlar' satırını yazar.

Yürütücü zaten Fable ise ek çağrı yapılmaz: Fable hem yönetir hem notu kendi yazar.
Fable'a ulaşılamazsa (kota, zaman aşımı) görev eski yönetimsiz akışla çalışır.

Ayar: jrc_policy.json → "director": {"enabled", "model", "effort", "timeout_s"}
      (`jrc director on|off`). Yalnızca standart kütüphane kullanır.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import jrc_context  # noqa: E402
import jrc_gate  # noqa: E402

DEFAULTS: dict[str, Any] = {"enabled": True, "model": "fable", "effort": "medium", "timeout_s": 300}
REPORT_DIR = Path.home() / ".cache" / "jroute" / "reports"
PLAN_LIMIT = 6000  # agy -p argüman sınırı (131KB) ve yürütücü bağlamı için
NO_NOTE = "YOK"


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    user = jrc_gate.load_policy().get("director")
    if isinstance(user, dict):
        cfg.update(user)
    return cfg


def set_enabled(enabled: bool) -> None:
    """Politika dosyasında yöneticiyi açar/kapatır (dosya yerinde yazılır, inode korunur)."""
    pf = jrc_gate._policy_file()
    try:
        user = json.loads(pf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        user = {}
    user.setdefault("director", {})["enabled"] = bool(enabled)
    pf.write_text(json.dumps(user, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_director_model(model: str, cfg: dict[str, Any]) -> bool:
    return str(cfg["model"]).lower() in str(model).lower()


def active_for(decision: dict[str, Any]) -> bool:
    """Bu karar için ayrı bir Fable yönetim adımı gerekli mi?"""
    cfg = config()
    if not cfg.get("enabled"):
        return False
    # Yürütücü Fable'ın kendisiyse zaten o yönetir ve notu o yazar.
    return not (decision.get("tool") == "claude" and _is_director_model(decision.get("model", ""), cfg))


def _ask_fable(prompt: str, cwd: Path, task: str) -> str:
    """Fable'ı salt-okunur araçlarla tek seferlik çalıştırır; yanıt metnini döndürür."""
    cfg = config()
    cmd = [
        "claude", "--model", str(cfg["model"]), "--effort", str(cfg["effort"]),
        "-p", "--tools", "Read,Grep,Glob",
        "--strict-mcp-config", "--no-session-persistence",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, cwd=str(cwd),
        timeout=float(cfg["timeout_s"]), env={**os.environ, "JEV_ORIGIN": "1"},
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        raise RuntimeError((proc.stderr or out or f"claude çıkış kodu {proc.returncode}").strip()[:300])
    try:
        jrc_gate.record(
            "director",
            {"tool": "claude", "model": str(cfg["model"]), "effort": str(cfg["effort"]),
             "latency_ms": round((time.perf_counter() - t0) * 1000, 1)},
            task,
        )
    except Exception:
        pass
    return out


def _executor_line(decision: dict[str, Any]) -> str:
    return f"{decision.get('tool')} · {decision.get('model')} · efor {decision.get('effort')}"


def plan(task: str, decision: dict[str, Any], cwd: Path) -> str:
    """Yürütücü için iş emrini Fable'a yazdırır."""
    prompt = f"""Sen JEV iş akışının yöneticisisin. Görevi sen YAPMIYORSUN; jrc'nin seçtiği başka bir ajan yapacak.
Senin işin o ajan için bir iş emri yazmak: işin nasıl yapılacağını sen belirliyorsun.

Çalışma dizini: {cwd}
Yürütücü ajan: {_executor_line(decision)}

Kullanıcının görevi:
<gorev>
{task}
</gorev>

Önce bu dizindeki `.jrc_context.md` dosyasını (varsa) oku; görevle ilgili önceki kararlar ve kurallar orada.
Gerekiyorsa ilgili dosyalara bak (yalnızca okuma araçların var). Emin olmadığın şeyi kesinmiş gibi yazma;
"şunu doğrula" diye yürütücüye bırak.

Yürütücü senin gördüklerini görmeyecek, yalnızca yazdığın metni ve görevi okuyacak. İş emrinde şunlar olsun:
- Amaç: bitince ne elde edilmiş olmalı ve bunun neye hizmet ettiği.
- Yol: hangi sırayla, hangi dosya/servis/adres üzerinde ne yapılacağı (bulduğun somut yolları ve adları yaz).
- Dikkat: dokunulmaması gerekenler, geri alınamaz adımlar, bu projede geçerli kurallar.
- Bitti ölçütü: işin gerçekten çalıştığının nasıl doğrulanacağı.

Görev basitse iş emri de birkaç satır olsun; adım uydurma. En fazla 40 satır.
Yalnızca iş emrini yaz: giriş cümlesi, açıklama ya da soru ekleme."""
    return _ask_fable(prompt, cwd, task)[:PLAN_LIMIT]


def new_report_path() -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return REPORT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.md"


def executor_brief(work_order: str, report_path: Path) -> str:
    """Yürütücünün sistem istemine / görev metnine eklenecek yönetici talimatı."""
    return (
        "\n\n[JEV yönetici (Fable) iş emri — bu görevi aşağıdaki plana göre yap. "
        "Plan gerçek durumla çelişirse gerçeğe uy ve farkı raporuna yaz.]\n"
        f"{work_order}\n\n"
        "[Hafıza kuralı] '.jrc_context.md' dosyasını oku ama ona YAZMA; 'Ortak Notlar' satırını iş bitince "
        "yönetici (Fable) yazacak. İşin sonunda şu dosyaya kısa bir rapor yaz (düz metin): "
        f"{report_path}\n"
        "Raporda: ne yaptın, hangi dosya/kayıt/adres değişti, neyi doğruladın, ne eksik ya da başarısız kaldı."
    )


def _run_executor(cmd: list[str], cwd: Path, capture: bool) -> tuple[int, str]:
    """Yürütücüyü çalıştırır; capture=True ise çıktıyı ekrana aktarırken son kısmını saklar."""
    if not capture:
        return subprocess.run(cmd, cwd=str(cwd)).returncode, ""
    tail = ""
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, text=True, errors="replace")
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            tail = (tail + line)[-6000:]
    finally:
        proc.stdout.close()
    return proc.wait(), tail


def _file_changes(cwd: Path) -> dict[str, list[str]]:
    """Görev öncesi kaydedilen anlık görüntüye göre değişen dosyalar (hafıza dosyaları hariç)."""
    try:
        old = jrc_context.load_state(cwd).get("file_snapshot", {})
        changes = jrc_context.detect_file_changes(old, jrc_context.scan_project_files(cwd))
    except Exception:
        return {}
    skip = {jrc_context.CONTEXT_FILENAME, jrc_context.STATE_FILENAME}
    return {k: [p for p in v if Path(p).name not in skip][:40] for k, v in changes.items() if v}


def write_note(
    task: str,
    decision: dict[str, Any],
    cwd: Path,
    work_order: str,
    report: str,
    output_tail: str,
    exit_code: int,
) -> str | None:
    """Fable'a 'Ortak Notlar' satırını yazdırır ve .jrc_context.md'ye ekler. Not yoksa None."""
    changes = _file_changes(cwd)
    prompt = f"""Sen JEV iş akışının yöneticisisin. Verdiğin iş emrini başka bir ajan uyguladı; şimdi sonucu denetleyip
projenin ortak hafızasına (`.jrc_context.md` → 'Ortak Notlar') girecek notu yazacaksın. Bu notu ileride aynı
projede çalışacak ajanlar okuyacak; onlara neyin değiştiğini ve neye dikkat etmeleri gerektiğini söylemeli.

Çalışma dizini: {cwd}
Yürütücü ajan: {_executor_line(decision)} (çıkış kodu: {exit_code})

Kullanıcının görevi:
<gorev>
{task}
</gorev>

Senin iş emrin:
<is_emri>
{work_order}
</is_emri>

Yürütücünün raporu (boşsa rapor bırakmadı):
<rapor>
{report[:6000]}
</rapor>

Yürütücü çıktısının sonu (yalnızca tek seferlik modda dolu):
<cikti>
{output_tail[-3000:]}
</cikti>

Çalışma dizininde değişen dosyalar: {json.dumps(changes, ensure_ascii=False) if changes else "yok / tespit edilmedi"}

Raporu olduğu gibi kabul etme: değişen dosyalara okuma araçlarınla bakıp iddiaları doğrulayabildiğin kadar doğrula.
Doğrulayamadığın şeyi "rapora göre" diye belirt. İş yarım kaldıysa ya da başarısızsa bunu ve kalan işi açıkça yaz.

Notu şu biçimde ver:
- Tek satır, düz metin; tarih/saat ekleme (sistem ekler). Uzunluk işin büyüklüğüyle orantılı olsun:
  küçük bir düzeltme için bir cümle yeter, üst sınır ~600 karakter.
- Somut ol: değişen dosya yolları, kayıt numaraları, adresler, alınan mimari karar ve nedeni.
- Yalnızca ileride işe yarayacak olanı yaz; zararsız yan etkileri, "kalan iş yok" gibi boş teyitleri
  ve iş emrinde zaten yazan kuralları tekrarlama.
- Görev yalnızca bir sorgu/kontrol idiyse ve kalıcı hiçbir şey değişmediyse sadece {NO_NOTE} yaz.
Yalnızca not satırını (ya da {NO_NOTE}) yaz; başka hiçbir şey ekleme."""
    note = " ".join(_ask_fable(prompt, cwd, task).split())
    if note.startswith("- "):
        note = note[2:]
    if not note or note.strip(" .`*").upper() == NO_NOTE:
        return None
    jrc_context.append_user_note(cwd, note)
    return note


def _say(msg: str) -> None:
    print(f"\033[35m🎬 Yönetici (Fable):\033[0m {msg}", file=sys.stderr)


def run(
    task: str,
    decision: dict[str, Any],
    cwd: Path,
    print_mode: bool,
    build_cmd: Callable[[str | None], list[str]],
) -> int:
    """Yönetimli çalıştırma: Fable iş emri → yürütücü → Fable notu. Yürütücünün çıkış kodunu döndürür.

    build_cmd(brief) seçilen yürütücünün komutunu üretir; brief=None yönetimsiz (eski) komuttur.
    """
    work_order = ""
    try:
        _say("iş emri hazırlanıyor…")
        work_order = plan(task, decision, cwd)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _say(f"\033[33miş emri alınamadı ({str(exc)[:160]}); görev yönetimsiz çalıştırılıyor.\033[0m")
    if not work_order:
        return subprocess.run(build_cmd(None), cwd=str(cwd)).returncode

    print(f"\033[2m{work_order}\033[0m\n", file=sys.stderr)
    report_path = new_report_path()
    cmd = build_cmd(executor_brief(work_order, report_path))

    exit_code, tail = 130, ""
    try:
        exit_code, tail = _run_executor(cmd, cwd, capture=print_mode)
    except KeyboardInterrupt:
        print(file=sys.stderr)

    try:
        report = report_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        report = ""
    try:
        _say("sonuç denetleniyor, ortak hafıza notu yazılıyor…")
        note = write_note(task, decision, cwd, work_order, report, tail, exit_code)
        _say(f"\033[32m✓ .jrc_context.md notu:\033[0m {note}" if note else "kalıcı değişiklik yok, not eklenmedi.")
    except KeyboardInterrupt:
        _say("not yazımı iptal edildi.")
    except Exception as exc:
        _say(f"\033[33mnot yazılamadı ({str(exc)[:160]}).\033[0m")
        if report:  # not kaybolmasın: yürütücü raporunu kaynağı belli olacak şekilde ekle
            jrc_context.append_user_note(
                cwd, f"(Fable yazamadı; {decision.get('model')} raporundan) " + " ".join(report.split())[:500]
            )
    return exit_code
