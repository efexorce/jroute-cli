"""JRC Yönetici Katmanı (jrc_director) — işin NASIL yapılacağını Opus yönetir.

Akış (jrc / jroute insan görevleri):
    1. jrc motoru + modeli + eforu seçer (router.analyze_task_with_jev) — değişmedi.
    2. Opus salt-okunur araçlarla projeye bakıp yürütücü için bir "iş emri" yazar.
    3. Seçilen yürütücü (claude/agy) iş emriyle çalışır; `.jrc_context.md`'ye YAZMAZ,
       bitince kısa raporunu ~/.cache/jroute/reports/ altına bırakır.
    4. Opus raporu + değişen dosyaları denetler ve `.jrc_context.md` içindeki
       'Ortak Notlar' satırını yazar.

Yürütücü zaten yönetici modelse (Opus) ek çağrı yapılmaz: hem yönetir hem notu kendi yazar.
Yöneticiye ulaşılamazsa (kota, zaman aşımı) görev eski yönetimsiz akışla çalışır.

Token tasarrufu (oturum yönetimi):
  - İş emri ve not aynı yönetici oturumunda yürür: not adımı `--resume` ile iş emri
    oturumuna devam eder; görev/iş emri/proje okuması yeniden gönderilmez, önbellekten gelir.
    Oturum görev başına açılır ve görev bitince bırakılır (sınırsız büyüyen geçmiş yok).
  - Basit görevlerde (efor low veya haiku/flash rotası) yönetici hiç çağrılmaz;
    yürütücü notu kendi yazar (skip_trivial).

Hız (prefetch): iş emri jrc kararını BEKLEMEZ; görev gelince start_prefetch() ile karar analiziyle
  paralel başlar. Karar yöneticiyi gerektirmezse resolve_prefetch() süreci öldürür.

Ayar: jrc_policy.json → "director": {"enabled", "model", "effort", "timeout_s",
      "skip_trivial", "reuse_session", "prefetch"}
      (`jrc director on|off`). Yalnızca standart kütüphane kullanır.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import jrc_context  # noqa: E402
import jrc_gate  # noqa: E402

DEFAULTS: dict[str, Any] = {
    "enabled": True, "model": "opus", "effort": "medium", "timeout_s": 300,
    "skip_trivial": True, "reuse_session": True, "prefetch": True,
}
TRIVIAL_ROUTES = {"claude_haiku", "agy_flash"}
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


def director_name() -> str:
    """'opus' / 'claude-opus-5-5' → 'Opus' (ekran ve istem metinleri için)."""
    m = str(config()["model"]).lower().replace("claude-", "")
    return m.split("-")[0].capitalize()


def _is_trivial(decision: dict[str, Any]) -> bool:
    return decision.get("effort") == "low" or decision.get("route_key") in TRIVIAL_ROUTES


def _is_director_model(model: str, cfg: dict[str, Any]) -> bool:
    return str(cfg["model"]).lower() in str(model).lower()


def active_for(decision: dict[str, Any]) -> bool:
    """Bu karar için ayrı bir yönetici (Opus) adımı gerekli mi?"""
    cfg = config()
    if not cfg.get("enabled"):
        return False
    # Basit görevde yönetici çağrısı (2 Opus isteği) işin kendisinden pahalı olur.
    if cfg.get("skip_trivial", True) and _is_trivial(decision):
        return False
    # Yürütücü yönetici modelin kendisiyse zaten o yönetir ve notu o yazar.
    return not (decision.get("tool") == "claude" and _is_director_model(decision.get("model", ""), cfg))


def _ask_director(prompt: str, cwd: Path, task: str, session: str | None = None, resume: bool = False,
                  on_start: Callable[[subprocess.Popen], None] | None = None) -> str:
    """Yönetici modeli salt-okunur araçlarla tek seferlik çalıştırır; yanıt metnini döndürür.

    session verilirse: resume=False → o kimlikle yeni oturum açar; resume=True → o oturuma devam eder.
    on_start(proc): süreç başlayınca çağrılır (ön-hazırlığın iptal edilebilmesi için).
    """
    cfg = config()
    cmd = [
        "claude", "--model", str(cfg["model"]), "--effort", str(cfg["effort"]),
        "-p", "--tools", "Read,Grep,Glob", "--strict-mcp-config",
    ]
    if session:
        cmd += ["--resume", session] if resume else ["--session-id", session]
    else:
        cmd.append("--no-session-persistence")
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(cwd), env={**os.environ, "JEV_ORIGIN": "1"},
    )
    if on_start:
        on_start(proc)
    try:
        stdout, stderr = proc.communicate(prompt, timeout=float(cfg["timeout_s"]))
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    out = (stdout or "").strip()
    if proc.returncode != 0 or not out:
        raise RuntimeError((stderr or out or f"claude çıkış kodu {proc.returncode}").strip()[:300])
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


def _executor_line(decision: dict[str, Any] | None) -> str:
    if not decision:
        return "henüz seçilmedi (jrc paralel olarak claude veya agy seçecek) — iş emrini ajandan bağımsız yaz"
    return f"{decision.get('tool')} · {decision.get('model')} · efor {decision.get('effort')}"


def plan(task: str, decision: dict[str, Any] | None, cwd: Path, session: str | None = None,
         on_start: Callable[[subprocess.Popen], None] | None = None,
         allowed_user_id: int | None = None) -> str:
    """Yürütücü için iş emrini yöneticiye yazdırır (session verilirse oturum açılır)."""
    emir_ctx = jrc_context.build_emir_context_block(task, allowed_user_id=allowed_user_id)
    emir_section = f"\nReferans verilen önceki emir bağlamı:\n{emir_ctx}\n" if emir_ctx else ""
    prompt = f"""Sen JEV iş akışının yöneticisisin. Görevi sen YAPMIYORSUN; jrc'nin seçtiği başka bir ajan yapacak.
Senin işin o ajan için bir iş emri yazmak: işin nasıl yapılacağını sen belirliyorsun.

Çalışma dizini: {cwd}
Yürütücü ajan: {_executor_line(decision)}

Kullanıcının görevi:
<gorev>
{task}
</gorev>
{emir_section}
Önce bu dizindeki `.jrc_context.md` dosyasını (varsa) oku; görevle ilgili önceki kararlar ve kurallar orada.
Gerekiyorsa ilgili dosyalara bak (yalnızca okuma araçların var). Emin olmadığın şeyi kesinmiş gibi yazma;
"şunu doğrula" diye yürütücüye bırak.

Yürütücü senin gördüklerini görmeyecek, yalnızca yazdığın metni ve görevi okuyacak. İş emrinde şunlar olsun:
- Amaç: bitince ne elde edilmiş olmalı ve bunun neye hizmet ettiği.
- Yol: hangi sırayla, hangi dosya/servis/adres üzerinde ne yapılacağı (bulduğun somut yolları ve adları yaz).
- Dikkat: dokunulmaması gerekenler, geri alınamaz adımlar, bu projede geçerli kurallar.
- Bitti ölçütü: işin gerçekten çalıştığının nasıl doğrulanacağı.

Token bütçesi sınırlı: yalnızca gerçekten gereken dosyaları aç, büyük dosyalarda Grep ile hedefe git.
Görev basitse iş emri de birkaç satır olsun; adım uydurma. En fazla 30 satır.
Yalnızca iş emrini yaz: giriş cümlesi, açıklama ya da soru ekleme."""
    return _ask_director(prompt, cwd, task, session=session, on_start=on_start)[:PLAN_LIMIT]


class Prefetch:
    """İş emrini jrc kararıyla PARALEL hazırlar: görev gelir gelmez yönetici yazmaya başlar.

    Karar geldiğinde yönetici gerekiyorsa run(prefetch=...) sonucu bekler; gerekmiyorsa
    (basit görev / yürütücü zaten yönetici model / yönetici kapalı) cancel() süreci öldürür.
    """

    def __init__(self, task: str, cwd: Path, allowed_user_id: int | None = None) -> None:
        self.session = str(uuid.uuid4()) if config().get("reuse_session", True) else None
        self.started = time.perf_counter()
        self._task, self._cwd, self._allowed_user_id = task, cwd, allowed_user_id
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self._result = ""
        self._error: BaseException | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def _on_start(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._proc = proc
            if self._cancelled:
                proc.kill()

    def _work(self) -> None:
        try:
            self._result = plan(
                self._task, None, self._cwd, session=self.session,
                on_start=self._on_start, allowed_user_id=self._allowed_user_id,
            )
        except BaseException as exc:  # noqa: BLE001 — iş parçacığında her hata sonuca taşınır
            self._error = exc

    def result(self) -> str:
        """İş emrini bekler ve döndürür; hata varsa yükseltir."""
        while self._thread.is_alive():
            self._thread.join(0.2)  # Ctrl+C ana iş parçacığında yakalanabilsin
        if self._error:
            raise self._error
        return self._result

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._proc and self._proc.poll() is None:
                self._proc.kill()


def start_prefetch(task: str, cwd: Path, allowed_user_id: int | None = None) -> Prefetch | None:
    """Yönetici açıksa ve ön-hazırlık izinliyse iş emrini hemen arka planda başlatır."""
    cfg = config()
    if not cfg.get("enabled") or not cfg.get("prefetch", True):
        return None
    try:
        return Prefetch(task, cwd, allowed_user_id=allowed_user_id)
    except Exception:
        return None


def resolve_prefetch(pf: Prefetch | None, decision: dict[str, Any]) -> Prefetch | None:
    """Karar geldi: yönetici gerekmiyorsa ön-hazırlığı iptal eder; gerekiyorsa onu döndürür."""
    if pf is None:
        return None
    if active_for(decision):
        return pf
    pf.cancel()
    return None


def new_report_path() -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return REPORT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.md"


def executor_brief(work_order: str, report_path: Path) -> str:
    """Yürütücünün sistem istemine / görev metnine eklenecek yönetici talimatı."""
    return (
        f"\n\n[JEV yönetici ({director_name()}) iş emri — bu görevi aşağıdaki plana göre yap. "
        "Plan gerçek durumla çelişirse gerçeğe uy ve farkı raporuna yaz.]\n"
        f"{work_order}\n\n"
        "[Hafıza kuralı] '.jrc_context.md' dosyasını oku ama ona YAZMA; 'Ortak Notlar' satırını iş bitince "
        f"yönetici ({director_name()}) yazacak. İşin sonunda şu dosyaya kısa bir rapor yaz (düz metin): "
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
    session: str | None = None,
) -> str | None:
    """Yöneticiye 'Ortak Notlar' satırını yazdırır ve .jrc_context.md'ye ekler. Not yoksa None.

    session verilirse iş emri oturumuna devam edilir: görev ve iş emri yeniden gönderilmez.
    """
    changes = _file_changes(cwd)
    changes_txt = json.dumps(changes, ensure_ascii=False) if changes else "yok / tespit edilmedi"
    if session:
        try:
            note = _clean_note(_ask_director(_note_prompt_resumed(decision, report, output_tail, exit_code, changes_txt),
                                             cwd, task, session=session, resume=True))
            if note:
                jrc_context.append_user_note(cwd, note)
            return note
        except Exception:
            pass  # oturum bulunamadı vb. → tam istemle tekrar dene
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

Çalışma dizininde değişen dosyalar: {changes_txt}

{NOTE_RULES}"""
    note = _clean_note(_ask_director(prompt, cwd, task))
    if note:
        jrc_context.append_user_note(cwd, note)
    return note


NOTE_RULES = f"""Raporu olduğu gibi kabul etme: değişen dosyalara okuma araçlarınla bakıp iddiaları doğrulayabildiğin kadar doğrula.
Doğrulayamadığın şeyi "rapora göre" diye belirt. İş yarım kaldıysa ya da başarısızsa bunu ve kalan işi açıkça yaz.

Notu şu biçimde ver:
- Tek satır, düz metin; tarih/saat ekleme (sistem ekler). Uzunluk işin büyüklüğüyle orantılı olsun:
  küçük bir düzeltme için bir cümle yeter, üst sınır ~600 karakter.
- Somut ol: değişen dosya yolları, kayıt numaraları, adresler, alınan mimari karar ve nedeni.
- Yalnızca ileride işe yarayacak olanı yaz; zararsız yan etkileri, "kalan iş yok" gibi boş teyitleri
  ve iş emrinde zaten yazan kuralları tekrarlama.
- Görev yalnızca bir sorgu/kontrol idiyse ve kalıcı hiçbir şey değişmediyse sadece {NO_NOTE} yaz.
Yalnızca not satırını (ya da {NO_NOTE}) yaz; başka hiçbir şey ekleme."""


def _note_prompt_resumed(decision: dict[str, Any], report: str, output_tail: str, exit_code: int,
                         changes_txt: str) -> str:
    """Aynı oturumda devam ederken kısa not istemi: görev ve iş emri zaten bağlamda."""
    return f"""Yürütücü ({_executor_line(decision)}) verdiğin iş emrini uyguladı (çıkış kodu: {exit_code}).
Şimdi sonucu denetleyip `.jrc_context.md` → 'Ortak Notlar' için notu yaz.

Yürütücünün raporu (boşsa rapor bırakmadı):
<rapor>
{report[:6000]}
</rapor>

Yürütücü çıktısının sonu (yalnızca tek seferlik modda dolu):
<cikti>
{output_tail[-3000:]}
</cikti>

Değişen dosyalar: {changes_txt}

{NOTE_RULES}"""


def _clean_note(raw: str) -> str | None:
    note = " ".join(raw.split())
    if note.startswith("- "):
        note = note[2:]
    if not note or note.strip(" .`*").upper() == NO_NOTE:
        return None
    return note


def _say(msg: str) -> None:
    print(f"\033[35m🎬 Yönetici ({director_name()}):\033[0m {msg}", file=sys.stderr)


def run(
    task: str,
    decision: dict[str, Any],
    cwd: Path,
    print_mode: bool,
    build_cmd: Callable[[str | None], list[str]],
    prefetch: Prefetch | None = None,
    allowed_user_id: int | None = None,
    cmd_wrapper: Callable[[list[str], Path | None], tuple[list[str], Path | None, Callable[[], None] | None]] | None = None,
) -> int:
    """Yönetimli çalıştırma: yönetici iş emri → yürütücü → yönetici notu (aynı oturum). Yürütücünün çıkış kodunu döndürür.

    build_cmd(brief) seçilen yürütücünün komutunu üretir; brief=None yönetimsiz (eski) komuttur.
    """
    work_order = ""
    try:
        if prefetch is not None:
            session = prefetch.session
            waited = time.perf_counter() - prefetch.started
            _say(f"iş emri (karar ile paralel, {waited:.0f} sn önce başladı) bekleniyor…")
            work_order = prefetch.result()
        else:
            session = str(uuid.uuid4()) if config().get("reuse_session", True) else None
            _say("iş emri hazırlanıyor…")
            work_order = plan(task, decision, cwd, session=session, allowed_user_id=allowed_user_id)
    except KeyboardInterrupt:
        if prefetch is not None:
            prefetch.cancel()
        raise
    except Exception as exc:
        _say(f"\033[33miş emri alınamadı ({str(exc)[:160]}); görev yönetimsiz çalıştırılıyor.\033[0m")
    if not work_order:
        raw_fallback = build_cmd(None)
        cleanup_fb: Callable[[], None] | None = None
        if cmd_wrapper is not None:
            raw_fallback, _, cleanup_fb = cmd_wrapper(raw_fallback, None)
        try:
            return subprocess.run(raw_fallback, cwd=str(cwd)).returncode
        finally:
            if cleanup_fb:
                cleanup_fb()

    print(f"\033[2m{work_order}\033[0m\n", file=sys.stderr)
    report_path = new_report_path()
    cleanup_fn: Callable[[], None] | None = None
    if cmd_wrapper is not None:
        report_path.touch(exist_ok=True)
        _, sandbox_report_path, cleanup_probe = cmd_wrapper([], report_path)
        if cleanup_probe:
            cleanup_probe()
        effective_report = sandbox_report_path or report_path
        cmd = build_cmd(executor_brief(work_order, effective_report))
        cmd, _, cleanup_fn = cmd_wrapper(cmd, report_path)
    else:
        cmd = build_cmd(executor_brief(work_order, report_path))

    exit_code, tail = 130, ""
    try:
        exit_code, tail = _run_executor(cmd, cwd, capture=print_mode)
    except KeyboardInterrupt:
        print(file=sys.stderr)
    finally:
        if cleanup_fn:
            cleanup_fn()

    try:
        report = report_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        report = ""
    try:
        _say("sonuç denetleniyor, ortak hafıza notu yazılıyor…")
        note = write_note(task, decision, cwd, work_order, report, tail, exit_code, session=session)
        _say(f"\033[32m✓ .jrc_context.md notu:\033[0m {note}" if note else "kalıcı değişiklik yok, not eklenmedi.")
    except KeyboardInterrupt:
        _say("not yazımı iptal edildi.")
    except Exception as exc:
        _say(f"\033[33mnot yazılamadı ({str(exc)[:160]}).\033[0m")
        if report:  # not kaybolmasın: yürütücü raporunu kaynağı belli olacak şekilde ekle
            jrc_context.append_user_note(
                cwd, f"({director_name()} yazamadı; {decision.get('model')} raporundan) " + " ".join(report.split())[:500]
            )
    return exit_code
