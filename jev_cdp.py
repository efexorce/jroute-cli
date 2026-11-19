#!/usr/bin/env python3
"""jev_cdp — ana Chrome'a TEK ve KALICI CDP bağlantısı (JEV).

Neden: Chrome (chrome://inspect "uzaktan hata ayıklama") her YENİ websocket
bağlantısında "İzin ver" penceresi açar. Her komutta yeni bağlantı açmak =
her tıklamada onay. Bu araç bir arka plan servisi (daemon) başlatır; servis
Chrome'a bir kez bağlanır (tek onay), sonra tüm komutlar unix soket üzerinden
o bağlantıyı kullanır. Servis açık kaldıkça sonraki görevlerde de onay gerekmez.

Kullanım:
  jev_cdp.py status                      servis durumu
  jev_cdp.py start | stop                servisi başlat / durdur
  jev_cdp.py tabs                        açık sekmeler
  jev_cdp.py eval  <url-parça> <js>      JS çalıştır (await destekli)
  jev_cdp.py shot  <url-parça> <dosya>   ekran görüntüsü (png)
  jev_cdp.py click <url-parça> <x> <y>   tıkla
  jev_cdp.py type  <url-parça> <metin>   metin yaz (odaktaki alana)
  jev_cdp.py key   <url-parça> <tuş>     Enter/Tab/Escape/Backspace...
  jev_cdp.py nav   <url-parça> <url>     sekmede adrese git
  jev_cdp.py download-dir <klasör>       indirmeleri bu klasöre izin ver
  jev_cdp.py raw <method> [json-params] [url-parça]   ham CDP komutu
"""
import asyncio, base64, json, os, socket, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlparse

SOCK = Path(os.environ.get("JEV_CDP_SOCK") or (Path.home() / ".cache" / "jev_cdp" / "cdp.sock"))
LOG = SOCK.with_name("daemon.log")
PORT_FILES = [Path.home() / ".config/google-chrome/DevToolsActivePort",
              Path.home() / "snap/chromium/common/chromium/DevToolsActivePort"]
APPROVE_WAIT = 300  # kullanıcının "İzin ver"e basması için süre (sn)


def _tcp_open(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ws_url():
    env = os.environ.get("JEV_CDP_WS")
    if env:
        u = urlparse(env)
        port = u.port or (443 if u.scheme == "wss" else 80)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise SystemExit(f"Geçersiz JEV_CDP_WS adresi: {env}")
        if not _tcp_open(u.hostname, port):
            raise SystemExit(
                f"Chrome kapalı veya Chrome'da uzaktan hata ayıklama kapalı "
                f"({u.hostname}:{port} portuna bağlanılamadı)"
            )
        return env
    stale = []
    for p in PORT_FILES:
        if p.exists():
            parts = p.read_text().split()
            if len(parts) >= 2:
                port, path = parts[:2]
                if port.isdigit() and _tcp_open("127.0.0.1", int(port)):
                    return f"ws://127.0.0.1:{port}{path}"
                stale.append(f"{p} (port {port} dinlemiyor)")
    if stale:
        raise SystemExit(
            "Chrome kapalı veya Chrome'da uzaktan hata ayıklama kapalı "
            f"(bayat DevToolsActivePort: {', '.join(stale)}; chrome://inspect/#remote-debugging açık mı?)"
        )
    raise SystemExit(
        "Chrome kapalı veya Chrome'da uzaktan hata ayıklama kapalı "
        "(DevToolsActivePort bulunamadı; chrome://inspect/#remote-debugging açık mı?)"
    )


# ---------------------------------------------------------------- daemon
class Daemon:
    def __init__(self):
        self.ws = None
        self.i = 0
        self.pending = {}
        self.sessions = {}  # targetId -> sessionId
        self.events = []    # son indirme olayları

    async def call(self, method, params=None, sid=None, timeout=120):
        self.i += 1
        mid = self.i
        msg = {"id": mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        await self.ws.send(json.dumps(msg))
        r = await asyncio.wait_for(fut, timeout)
        if "error" in r:
            raise RuntimeError(r["error"])
        return r["result"]

    async def reader(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            elif m.get("method") == "Target.detachedFromTarget":
                sid = m["params"].get("sessionId")
                self.sessions = {k: v for k, v in self.sessions.items() if v != sid}
            elif m.get("method", "").startswith("Browser.download"):
                self.events = (self.events + [m])[-50:]
        raise ConnectionError("Chrome bağlantısı kapandı")

    async def pages(self):
        t = await self.call("Target.getTargets")
        return [x for x in t["targetInfos"] if x["type"] == "page"]

    async def session(self, match):
        pages = await self.pages()
        hit = [x for x in pages if match in x["url"] or match == x["targetId"]]
        if not hit:
            raise RuntimeError(f"'{match}' içeren sekme yok")
        tid = hit[0]["targetId"]
        if tid not in self.sessions:
            r = await self.call("Target.attachToTarget", {"targetId": tid, "flatten": True})
            self.sessions[tid] = r["sessionId"]
        return self.sessions[tid]

    async def handle(self, req):
        a = req["args"]
        cmd = a[0]
        if cmd == "ping":
            return "ok"
        if cmd == "tabs":
            return "\n".join(f"{x['targetId']} {x['url'][:140]} | {x['title'][:50]}" for x in await self.pages())
        if cmd == "download-dir":
            os.makedirs(a[1], exist_ok=True)
            await self.call("Browser.setDownloadBehavior",
                            {"behavior": "allowAndName", "downloadPath": os.path.abspath(a[1]), "eventsEnabled": True})
            return "ok"
        if cmd == "events":
            ev, self.events = self.events, []
            return json.dumps(ev, ensure_ascii=False)
        if cmd == "raw":
            params = json.loads(a[2]) if len(a) > 2 else {}
            sid = await self.session(a[3]) if len(a) > 3 else None
            return json.dumps(await self.call(a[1], params, sid), ensure_ascii=False)
        sid = await self.session(a[1])
        if cmd == "eval":
            r = await self.call("Runtime.evaluate", {"expression": a[2], "awaitPromise": True,
                                                     "returnByValue": True}, sid, timeout=600)
            if "exceptionDetails" in r:
                return "HATA: " + json.dumps(r["exceptionDetails"], ensure_ascii=False)[:3000]
            return json.dumps(r.get("result", {}).get("value"), ensure_ascii=False)
        if cmd == "shot":
            r = await self.call("Page.captureScreenshot", {"format": "png"}, sid)
            Path(a[2]).write_bytes(base64.b64decode(r["data"]))
            return "ok " + a[2]
        if cmd == "click":
            x, y = float(a[2]), float(a[3])
            for tp in ("mousePressed", "mouseReleased"):
                await self.call("Input.dispatchMouseEvent",
                                {"type": tp, "x": x, "y": y, "button": "left", "clickCount": 1}, sid)
            return "clicked"
        if cmd == "type":
            await self.call("Input.insertText", {"text": a[2]}, sid)
            return "typed"
        if cmd == "key":
            k = a[2]
            codes = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8}
            for tp in ("keyDown", "keyUp"):
                await self.call("Input.dispatchKeyEvent", {"type": tp, "key": k, "code": k,
                                "windowsVirtualKeyCode": codes.get(k, 0),
                                **({"text": "\r"} if k == "Enter" and tp == "keyDown" else {})}, sid)
            return "key " + k
        if cmd == "nav":
            await self.call("Page.navigate", {"url": a[2]}, sid)
            return "nav"
        raise RuntimeError(f"bilinmeyen komut: {cmd}")

    async def client(self, reader, writer):
        try:
            req = json.loads(await reader.readline())
            try:
                out = {"ok": True, "out": await self.handle(req)}
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "out": f"{type(e).__name__}: {e}"}
            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()

    async def run(self):
        import websockets
        print(time.strftime("%F %T"), "Chrome'a bağlanılıyor (İzin ver bekleniyor)...", flush=True)
        self.ws = await websockets.connect(ws_url(), max_size=None, open_timeout=APPROVE_WAIT,
                                           ping_interval=None)
        print(time.strftime("%F %T"), "bağlandı", flush=True)
        SOCK.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self.client, path=str(SOCK))
        os.chmod(SOCK, 0o600)
        try:
            await self.reader()
        finally:
            server.close()
            SOCK.unlink(missing_ok=True)


# ---------------------------------------------------------------- client
def send(args, timeout=700):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(str(SOCK))
    s.sendall((json.dumps({"args": args}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(1 << 20)
        if not chunk:
            break
        buf += chunk
    return json.loads(buf)


def alive():
    try:
        return send(["ping"], timeout=5)["ok"]
    except (OSError, ValueError, KeyError):
        return False


def _last_log_line(since=0):
    try:
        with open(LOG, "rb") as f:
            f.seek(since)
            text = f.read().decode("utf-8", errors="replace").strip()
        if not text:
            text = LOG.read_text(errors="replace").strip()
        return text.splitlines()[-1] if text else "(log boş)"
    except OSError:
        return "(log okunamadı)"


def _notify(msg, urgent=False, replace_id=None, expire_ms=None):
    cmd = ["notify-send", "-p"]
    if urgent:
        cmd += ["-u", "critical"]
    else:
        cmd += ["-u", "normal"]
    if replace_id and str(replace_id).isdigit():
        cmd += ["-r", str(replace_id)]
    if expire_ms is not None:
        cmd += ["-t", str(expire_ms)]
    cmd += ["JEV", msg]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        nid = (r.stdout or "").strip()
        return nid if nid.isdigit() else None
    except (OSError, subprocess.SubprocessError):
        return None


def start():
    if alive():
        return True
    url = ws_url()  # çözülemezse veya TCP port kapalıysa bildirim göndermeden çıkar
    SOCK.parent.mkdir(parents=True, exist_ok=True)
    log_pos = LOG.stat().st_size if LOG.exists() else 0
    env = os.environ.copy()
    env["JEV_CDP_WS"] = url
    with open(LOG, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_daemon"],
            stdout=lf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, env=env,
        )
    # Onay penceresi gerektirmeyen bağlantılar (--remote-debugging-port) veya
    # anında düşen hatalar için kısa ön bekleme: gereksiz bildirim gönderme.
    pre_end = time.time() + 1.0
    while time.time() < pre_end:
        if alive():
            return True
        if proc.poll() is not None:
            raise SystemExit(f"Chrome bağlantısı kurulamadı: {_last_log_line(log_pos)}")
        time.sleep(0.1)

    nid = _notify(
        "Chrome penceresinde üstte çıkan (adres çubuğu altında / chrome://inspect) "
        "'İzin ver' düğmesine BİR KEZ tıklayın",
        urgent=True,
    )
    print(
        "Chrome penceresinde üstte çıkan tek seferlik 'İzin ver' onayı bekleniyor...",
        file=sys.stderr, flush=True,
    )
    end = time.time() + APPROVE_WAIT + 10
    while time.time() < end:
        if alive():
            if nid:
                _notify("Chrome bağlantısı onaylandı.", replace_id=nid, expire_ms=2500)
            return True
        if proc.poll() is not None:
            _notify("Onay alınamadı, işlem iptal", replace_id=nid, expire_ms=8000)
            raise SystemExit(f"Bağlantı kurulamadı (daemon kapandı): {_last_log_line(log_pos)}")
        time.sleep(0.5)
    proc.terminate()
    _notify("Onay alınamadı, işlem iptal", replace_id=nid, expire_ms=8000)
    raise SystemExit("Onay alınamadı, işlem iptal; log: " + str(LOG))


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return
    if a[0] == "_daemon":
        asyncio.run(Daemon().run())
        return
    if a[0] == "status":
        print("bağlı" if alive() else "kapalı")
        return
    if a[0] == "stop":
        subprocess.run(["pkill", "-f", f"{os.path.abspath(__file__)} _daemon"])
        SOCK.unlink(missing_ok=True)
        print("durduruldu")
        return
    start()
    if a[0] == "start":
        print("bağlı")
        return
    r = send(a)
    print(r["out"])
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
