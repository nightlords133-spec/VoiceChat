#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoiceChat — голосовой чат в браузере для VPS.
Автор: NightLord РВЕ

Без внешних зависимостей: только Python 3.7+ и openssl (для самоподписанного сертификата).
Работает на отдельном порту (по умолчанию 8443) и не трогает VPN / 3x-ui / Xray.
"""
import base64
import hashlib
import hmac
import json
import math
import os
import queue
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
DATA = os.environ.get("VC_DATA") or os.path.join(BASE, "data")
MEDIA = os.path.join(DATA, "media")

HOST = os.environ.get("VC_HOST", "0.0.0.0")
PORT = int(os.environ.get("VC_PORT", "8443"))
PASSWORD = os.environ.get("VC_PASSWORD", "")
TLS = os.environ.get("VC_TLS", "1") != "0"
SECURE_COOKIE = os.environ.get("VC_SECURE_COOKIE", "1" if TLS else "0") == "1"
CERT = os.environ.get("VC_CERT") or os.path.join(DATA, "cert.pem")
KEY = os.environ.get("VC_KEY") or os.path.join(DATA, "key.pem")
MAX_HISTORY = int(os.environ.get("VC_HISTORY", "1000"))
MAX_AUDIO = 20 * 1024 * 1024          # 20 МБ на одно голосовое
MAX_DURATION = 600                     # 10 минут
TOKEN_TTL = 30 * 86400                 # вход действует 30 дней

CT_EXT = {
    "audio/webm": "webm", "video/webm": "webm",
    "audio/ogg": "ogg", "application/ogg": "ogg",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
    "audio/mpeg": "mp3",
}
EXT_MIME = {"webm": "audio/webm", "ogg": "audio/ogg", "m4a": "audio/mp4",
            "aac": "audio/aac", "mp3": "audio/mpeg"}
MEDIA_RE = re.compile(r"^[0-9a-f]{16}\.(webm|ogg|m4a|aac|mp3)$")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- секрет / токены
def load_secret():
    path = os.path.join(DATA, "secret.key")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(secrets.token_bytes(32))
        os.chmod(path, 0o600)
    with open(path, "rb") as f:
        raw = f.read()
    # пароль входит в ключ: смена пароля разлогинит всех
    return hashlib.sha256(raw + PASSWORD.encode()).digest()


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def ub64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(name):
    payload = b64u(json.dumps({"n": name, "e": int(time.time()) + TOKEN_TTL}).encode())
    sig = b64u(hmac.new(SECRET, payload.encode(), hashlib.sha256).digest())
    return payload + "." + sig


def check_token(tok):
    try:
        payload, sig = tok.split(".")
        good = b64u(hmac.new(SECRET, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        d = json.loads(ub64u(payload))
        if d["e"] < time.time():
            return None
        return d["n"]
    except Exception:
        return None


# ---------------------------------------------------------------- хранилище
class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.path = os.path.join(DATA, "messages.json")
        self.msgs = []
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.msgs = json.load(f)
            except Exception as e:
                log("Не удалось прочитать историю:", e)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.msgs, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    @staticmethod
    def _drop_file(m):
        if m.get("file"):
            try:
                os.remove(os.path.join(MEDIA, m["file"]))
            except OSError:
                pass

    def add(self, m):
        with self.lock:
            self.msgs.append(m)
            while len(self.msgs) > MAX_HISTORY:
                self._drop_file(self.msgs.pop(0))
            self._save()

    def delete(self, mid, user):
        with self.lock:
            for i, m in enumerate(self.msgs):
                if m["id"] == mid:
                    if m["user"] != user:
                        return False
                    self._drop_file(self.msgs.pop(i))
                    self._save()
                    return True
        return False

    def all(self):
        with self.lock:
            return list(self.msgs)


# ---------------------------------------------------------------- рассылка (SSE)
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = {}

    def add(self, q, name):
        with self.lock:
            self.clients[q] = name
        self.send("online", {"users": self.online()})

    def remove(self, q):
        with self.lock:
            self.clients.pop(q, None)
        self.send("online", {"users": self.online()})

    def online(self):
        with self.lock:
            return sorted(set(self.clients.values()), key=str.lower)

    def send(self, event, data):
        payload = "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))
        with self.lock:
            qs = list(self.clients)
        for q in qs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


# ---------------------------------------------------------------- защита входа
class Limiter:
    def __init__(self, limit=10, window=600):
        self.limit, self.window = limit, window
        self.fails = {}
        self.lock = threading.Lock()

    def blocked(self, ip):
        now = time.time()
        with self.lock:
            lst = [t for t in self.fails.get(ip, []) if now - t < self.window]
            self.fails[ip] = lst
            return len(lst) >= self.limit

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "VoiceChat"
    sys_version = ""
    timeout = 60

    def log_message(self, fmt, *args):
        pass

    # --- утилиты
    def user(self):
        c = cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        return check_token(c["vc"].value) if "vc" in c else None

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def send_json(self, code, obj, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.common_headers()
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, limit):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > limit:
            return None
        return self.rfile.read(n)

    def read_json(self, limit=8192):
        raw = self.read_body(limit)
        if raw is None:
            return None
        try:
            d = json.loads(raw.decode("utf-8"))
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def cookie(self, value, max_age):
        c = "vc=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (value, max_age)
        return c + ("; Secure" if SECURE_COOKIE else "")

    # --- GET
    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            return self.static("index.html", "text/html; charset=utf-8")
        if p == "/api/me":
            u = self.user()
            return self.send_json(200, {"name": u}) if u else self.send_json(401, {"error": "auth"})
        u = self.user()
        if not u:
            return self.send_json(401, {"error": "auth"})
        if p == "/api/messages":
            return self.send_json(200, {"messages": STORE.all(), "me": u})
        if p == "/api/events":
            return self.sse(u)
        if p.startswith("/media/"):
            return self.media(p[len("/media/"):])
        self.send_json(404, {"error": "not found"})

    def static(self, name, ctype):
        with open(os.path.join(STATIC, name), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; media-src 'self' blob:; "
                         "img-src 'self' data:; connect-src 'self'")
        self.common_headers()
        self.end_headers()
        self.wfile.write(body)

    def sse(self, u):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.common_headers()
        self.end_headers()
        q = queue.Queue(maxsize=500)
        HUB.add(q, u)
        try:
            self.wfile.write(b"retry: 3000\n\nevent: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=20)
                except queue.Empty:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            HUB.remove(q)

    def media(self, name):
        if not MEDIA_RE.match(name):
            return self.send_json(404, {"error": "not found"})
        path = os.path.join(MEDIA, name)
        if not os.path.exists(path):
            return self.send_json(404, {"error": "not found"})
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
        if m and (m.group(1) or m.group(2)):
            s, e = m.groups()
            if s == "":
                start = max(0, size - int(e))
            else:
                start = int(s)
                end = min(int(e), size - 1) if e else size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", EXT_MIME[name.rsplit(".", 1)[1]])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.common_headers()
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # --- POST
    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/login":
            return self.login()
        if p == "/api/logout":
            return self.send_json(200, {"ok": True}, cookie=self.cookie("", 0))
        u = self.user()
        if not u:
            return self.send_json(401, {"error": "auth"})
        if p == "/api/voice":
            return self.voice(u)
        if p == "/api/text":
            return self.text(u)
        if p == "/api/delete":
            return self.delete(u)
        self.send_json(404, {"error": "not found"})

    def login(self):
        ip = self.client_address[0]
        if LIMITER.blocked(ip):
            return self.send_json(429, {"error": "Слишком много попыток. Подождите 10 минут."})
        d = self.read_json() or {}
        name = re.sub(r"\s+", " ", str(d.get("name", ""))).strip()[:32]
        pw = str(d.get("password", ""))
        if not name:
            return self.send_json(400, {"error": "Введите имя"})
        if not hmac.compare_digest(pw.encode(), PASSWORD.encode()):
            LIMITER.fail(ip)
            log("Неудачный вход с", ip)
            return self.send_json(403, {"error": "Неверный пароль"})
        log("Вход:", name, ip)
        self.send_json(200, {"name": name}, cookie=self.cookie(make_token(name), TOKEN_TTL))

    def voice(self, u):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = CT_EXT.get(ctype)
        if not ext:
            return self.send_json(415, {"error": "Неподдерживаемый формат: " + ctype})
        data = self.read_body(MAX_AUDIO)
        if not data:
            return self.send_json(413, {"error": "Файл пустой или больше 20 МБ"})
        try:
            dur = float(parse_qs(urlparse(self.path).query).get("dur", ["0"])[0])
            if not math.isfinite(dur):
                dur = 0.0
        except ValueError:
            dur = 0.0
        dur = max(0.0, min(dur, MAX_DURATION))
        mid = secrets.token_hex(8)
        fn = "%s.%s" % (mid, ext)
        with open(os.path.join(MEDIA, fn), "wb") as f:
            f.write(data)
        m = {"id": mid, "type": "voice", "user": u, "ts": int(time.time() * 1000),
             "file": fn, "dur": round(dur, 1)}
        STORE.add(m)
        HUB.send("message", m)
        self.send_json(200, m)

    def text(self, u):
        d = self.read_json(16384) or {}
        t = str(d.get("text", "")).strip()[:2000]
        if not t:
            return self.send_json(400, {"error": "Пустое сообщение"})
        m = {"id": secrets.token_hex(8), "type": "text", "user": u,
             "ts": int(time.time() * 1000), "text": t}
        STORE.add(m)
        HUB.send("message", m)
        self.send_json(200, m)

    def delete(self, u):
        d = self.read_json() or {}
        mid = str(d.get("id", ""))
        if not STORE.delete(mid, u):
            return self.send_json(403, {"error": "Можно удалять только свои сообщения"})
        HUB.send("delete", {"id": mid})
        self.send_json(200, {"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Обрыв соединения / отказ браузера от самоподписанного сертификата — не ошибка
        err = sys.exc_info()[1]
        if not isinstance(err, (OSError, ssl.SSLError, ValueError)):
            log("Ошибка обработки запроса от", client_address[0], repr(err))


def ensure_cert():
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    if os.environ.get("VC_CERT") or os.environ.get("VC_KEY"):
        sys.exit("Не найден сертификат VC_CERT/VC_KEY: %s / %s" % (CERT, KEY))
    log("Создаю самоподписанный сертификат…")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", KEY, "-out", CERT, "-days", "3650", "-subj", "/CN=voicechat"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.chmod(KEY, 0o600)


def main():
    global SECRET, STORE, HUB, LIMITER
    if not PASSWORD:
        sys.exit("Задайте пароль в переменной VC_PASSWORD")
    os.makedirs(MEDIA, exist_ok=True)
    SECRET = load_secret()
    STORE, HUB, LIMITER = Store(), Hub(), Limiter()
    srv = Server((HOST, PORT), Handler)
    if TLS:
        ensure_cert()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CERT, KEY)
        # рукопожатие выполняется в потоке клиента, а не в главном цикле
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    log("VoiceChat (NightLord РВЕ) слушает %s://%s:%d" % ("https" if TLS else "http", HOST, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
