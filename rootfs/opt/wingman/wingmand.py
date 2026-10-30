#!/usr/bin/env python3
"""wingmand: the Wingman Computer's own box services. Python standard library only.

  1337  exec    POST /exec    run a command as the box user, get its output back
                GET  /health  the desktop health report (also written to desktop-health.json)
                GET  /screenshot  the screen as a PNG
  1338  pty     GET  /pty     WebSocket terminal: binary frames carry bytes both ways,
                              a text frame {"cols":N,"rows":N} resizes

Every request needs the box token: "Authorization: Bearer <token>". A browser WebSocket,
which cannot set headers, may send it as the subprotocol "wingman.bearer.<token>".
The token is read from /etc/wingman/token. It is never logged.
"""
import base64
import fcntl
import hashlib
import hmac
import http.server
import ipaddress
import json
import math
import os
import pty
import pwd
import select
import signal
import socket
import socketserver
import struct
import subprocess
import termios
import threading
import time
import urllib.parse

BIND = os.environ.get("WINGMAN_BIND", "127.0.0.1")
EXEC_PORT = int(os.environ.get("WINGMAN_EXEC_PORT", "1337"))
PTY_PORT = int(os.environ.get("WINGMAN_PTY_PORT", "1338"))
TOKEN_FILE = os.environ.get("WINGMAN_TOKEN_FILE", "/etc/wingman/token")
HEALTH_FILE = "/run/wingman/desktop-health.json"
DISPLAY = os.environ.get("DISPLAY", ":1")
MAX_BODY = 1 << 20          # 1 MiB request body
MAX_OUTPUT = 4 << 20        # 4 MiB of stdout and of stderr each
MAX_TIMEOUT = 3600
VERSION = "1"

# The parts of the desktop the health report checks: name -> process name in /proc/<pid>/comm.
PROCESSES = {"Xvfb", "xfwm4", "xfce4-panel", "plank", "picom", "x11vnc", "websockify"}
PORTS = {"vnc": 5900, "vnc_view": 5901, "novnc": 6080, "novnc_view": 6081,
         "exec": EXEC_PORT, "pty": PTY_PORT}

STARTED = time.time()


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), msg, flush=True)


def read_token():
    with open(TOKEN_FILE, encoding="ascii") as f:
        tok = f.read().strip()
    if len(tok) < 16:
        raise SystemExit("wingmand: token file is missing or too short")
    return tok.encode()


TOKEN = read_token()


def token_ok(value):
    """Constant-time check of a presented token (bytes or str)."""
    if not value:
        return False
    if isinstance(value, str):
        value = value.encode()
    return hmac.compare_digest(value, TOKEN)


def bearer(header):
    if header and header[:7].lower() == "bearer ":
        return header[7:].strip()
    return None


def loopback_authority(authority):
    """Parse a Host authority without DNS, userinfo, paths or ambiguous ports."""
    if not authority or any(c in authority for c in "/\\@?# \t\r\n"):
        return None
    try:
        parsed = urllib.parse.urlsplit("//" + authority)
        hostname, port = parsed.hostname, parsed.port
        if not hostname or (port is not None and not 1 <= port <= 65535):
            return None
        if hostname.lower() not in ("localhost", "localhost."):
            if not ipaddress.ip_address(hostname).is_loopback:
                return None
        return hostname.lower(), port or 80
    except ValueError:
        return None


def request_guard(host, origin):
    authority = loopback_authority(host)
    if authority is None:
        return 421, "a loopback Host is required"
    if origin is not None:
        try:
            parsed = urllib.parse.urlsplit(origin)
            valid = (parsed.scheme == "http" and not parsed.path and not parsed.query
                     and not parsed.fragment and loopback_authority(parsed.netloc) == authority)
        except ValueError:
            valid = False
        if not valid:
            return 403, "a same-origin loopback client is required"
    return None


def child_env():
    env = {
        "HOME": "/home/box", "USER": "box", "LOGNAME": "box", "SHELL": "/bin/bash",
        "PATH": "/home/box/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"), "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "TZ": os.environ.get("TZ", "America/Los_Angeles"),
        "DISPLAY": DISPLAY, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000"),
        "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
        "XDG_CURRENT_DESKTOP": "XFCE", "TERM": "xterm-256color",
    }
    return {k: v for k, v in env.items() if v}


# ---------------------------------------------------------------- health

def running_comms():
    names = set()
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/comm") as f:
                    names.add(f.read().strip())
            except OSError:
                pass
    return names


def listening_ports():
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    parts = line.split()
                    if parts[3] == "0A":  # LISTEN
                        ports.add(int(parts[1].rsplit(":", 1)[1], 16))
        except OSError:
            pass
    return ports


def screen_size():
    try:
        out = subprocess.run(["xdpyinfo", "-display", DISPLAY], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if "dimensions:" in line:
                return line.split()[1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def health():
    comms = running_comms()
    ports = listening_ports()
    listen = {k: (v in ports) for k, v in PORTS.items()}
    size = screen_size()
    required = [PROCESSES <= comms, all(listen.values()), size == "1920x1200"]
    return {
        "ok": all(required),
        "version": VERSION,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "uptime_s": round(time.time() - STARTED, 1),
        "screen": size,
        "tz": os.environ.get("TZ", "America/Los_Angeles"),
        "locale": os.environ.get("LANG", "C.UTF-8"),
        "user": pwd.getpwuid(os.getuid()).pw_name,
        "uid": os.getuid(),
        "processes": sorted(comms),
        "image_digest": os.environ.get("WINGMAN_IMAGE_DIGEST", ""),
        "listening": listen,
    }


def health_writer():
    while True:
        try:
            data = json.dumps(health(), indent=1).encode()
            tmp = HEALTH_FILE + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, HEALTH_FILE)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            log(f"health: {type(e).__name__}")
        time.sleep(5)


# ---------------------------------------------------------------- screenshot

def screenshot_png():
    """Keep Xlib's fatal display errors inside a short-lived capture process."""
    return subprocess.run(["python3", "-I", "/opt/wingman/screenshot.py", DISPLAY],
                          capture_output=True, timeout=20, check=True).stdout


# ---------------------------------------------------------------- exec (1337)

def run_command(req):
    if "argv" in req:
        argv = req["argv"]
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
            raise ValueError("argv must be a non-empty list of strings")
    elif isinstance(req.get("cmd"), str):
        argv = ["/bin/bash", "-lc", req["cmd"]]
    else:
        raise ValueError("send cmd (a string) or argv (a list)")
    cwd = req.get("cwd", "/home/box")
    timeout = float(req.get("timeout", 120))
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT:
        raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT} seconds")
    if not isinstance(cwd, str):
        raise ValueError("cwd must be a path string")
    env = child_env()
    extra = req.get("env", {})
    if not isinstance(extra, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                            for k, v in extra.items()):
        raise ValueError("env must map strings to strings")
    env.update(extra)

    stdin = req.get("stdin")
    if stdin is not None and not isinstance(stdin, str):
        raise ValueError("stdin must be a string")
    if "detach" in req and not isinstance(req["detach"], bool):
        raise ValueError("detach must be a boolean")
    if req.get("detach"):  # for windows and other long runners: setsid -f leaves no child behind
        rc = subprocess.run(["setsid", "-f"] + argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode
        return {"detached": True, "exit_code": rc}

    t0 = time.time()
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, start_new_session=True)
    timed_out = False
    try:
        out, err = p.communicate(stdin.encode() if isinstance(stdin, str) else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # owner-checked: this process group was created by Popen(start_new_session=True) above.
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
    return {
        "exit_code": p.returncode,
        "stdout": out[:MAX_OUTPUT].decode("utf-8", "replace"),
        "stderr": err[:MAX_OUTPUT].decode("utf-8", "replace"),
        "truncated": len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT,
        "timed_out": timed_out,
        "duration_ms": int((time.time() - t0) * 1000),
    }


class ExecHandler(http.server.BaseHTTPRequestHandler):
    server_version = "wingmand/" + VERSION
    sys_version = ""

    def log_message(self, fmt, *args):  # one short line, never headers or bodies
        log("exec request completed")

    def send_json(self, code, obj, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def authorized(self):
        if len(self.headers.get_all("Authorization", [])) != 1 or not token_ok(
                bearer(self.headers.get("Authorization"))):
            self.send_json(401, {"error": "a valid box token is required"}, {"WWW-Authenticate": "Bearer"})
            return False
        if len(self.headers.get_all("Host", [])) != 1:
            self.send_json(421, {"error": "one loopback Host is required"})
            return False
        if len(self.headers.get_all("Origin", [])) > 1:
            self.send_json(403, {"error": "one same-origin client is required"})
            return False
        refused = request_guard(self.headers.get("Host"), self.headers.get("Origin"))
        if refused:
            self.send_json(refused[0], {"error": refused[1]})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            h = health()
            self.send_json(200 if h["ok"] else 503, h)
        elif path == "/screenshot":
            try:
                png = screenshot_png()
            except (OSError, ValueError, struct.error, subprocess.SubprocessError) as e:
                return self.send_json(500, {"error": f"screenshot failed: {type(e).__name__}"})
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png)
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.authorized():
            return
        if urllib.parse.urlsplit(self.path).path != "/exec":
            return self.send_json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            return self.send_json(413, {"error": "body too large"})
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(req, dict):
                raise ValueError("send a JSON object")
            self.send_json(200, run_command(req))
        except (ValueError, TypeError, OSError) as e:
            self.send_json(400, {"error": str(e)})
        except subprocess.SubprocessError:
            self.send_json(500, {"error": "the command could not be started"})

    def unsupported_method(self):
        if self.authorized():
            self.send_json(405, {"error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = unsupported_method


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------- pty (1338), a minimal WebSocket

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_send(sock, opcode, payload):
    n = len(payload)
    if n < 126:
        head = struct.pack(">BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack(">BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack(">BBQ", 0x80 | opcode, 127, n)
    sock.sendall(head + payload)


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def ws_recv(sock, control=None):
    """Read one whole message. Returns (opcode, payload). Client frames must be masked."""
    message, first_op = b"", None
    while True:
        b1, b2 = recv_exact(sock, 2)
        fin, op, masked, n = b1 & 0x80, b1 & 0x0F, b2 & 0x80, b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", recv_exact(sock, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", recv_exact(sock, 8))[0]
        if b1 & 0x70 or not masked or n > MAX_BODY or len(message) + n > MAX_BODY:
            raise ConnectionError("bad frame")
        mask = recv_exact(sock, 4)
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(recv_exact(sock, n)))
        if op >= 8:                       # control frames come whole, between fragments
            if not fin or n > 125 or op not in (8, 9, 10):
                raise ConnectionError("bad control frame")
            if op == 9:
                if control:
                    control(10, data)
                else:
                    ws_send(sock, 10, data)
                continue
            if op == 10:
                continue
            return op, data
        if op not in (0, 1, 2) or (first_op is None and op == 0) or (first_op is not None and op != 0):
            raise ConnectionError("bad continuation")
        if first_op is None:
            first_op = op
        message += data
        if fin:
            return first_op, message


class PtyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(30)
        try:
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = sock.recv(1)
                if not chunk or len(raw) > 16384:
                    return
                raw += chunk
        except OSError:
            return
        head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        try:
            method, target, _ = head[0].split(" ", 2)
        except ValueError:
            return
        headers = {}
        duplicate = False
        for line in head[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                name = k.strip().lower()
                duplicate |= name in headers
                headers[name] = v.strip()

        def refuse(code, reason):
            sock.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())

        protocols = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        sub = next((p for p in protocols if p.startswith("wingman.bearer.")), None)
        presented = bearer(headers.get("authorization")) or (sub[len("wingman.bearer."):] if sub else None)
        if not token_ok(presented):
            log("pty refused: no valid token")
            return refuse(401, "Unauthorized")
        if duplicate:
            return refuse(400, "Bad Request")
        refused = request_guard(headers.get("host"), headers.get("origin"))
        if refused:
            return refuse(refused[0], "Misdirected Request" if refused[0] == 421 else "Forbidden")
        url = urllib.parse.urlsplit(target)
        if method != "GET" or url.path != "/pty":
            return refuse(404, "Not Found")
        key = headers.get("sec-websocket-key")
        try:
            valid_key = key is not None and len(base64.b64decode(key, validate=True)) == 16
        except ValueError:
            valid_key = False
        if (headers.get("upgrade", "").lower() != "websocket" or not valid_key
                or "upgrade" not in {p.strip().lower() for p in headers.get("connection", "").split(",")}
                or headers.get("sec-websocket-version") != "13"):
            return refuse(400, "Bad Request")
        q = urllib.parse.parse_qs(url.query)
        try:
            cols = int(q.get("cols", ["120"])[0])
            rows = int(q.get("rows", ["32"])[0])
        except ValueError:
            return refuse(400, "Bad Request")
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        reply = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n")
        if sub:
            reply += "Sec-WebSocket-Protocol: wingman.bearer\r\n" if "wingman.bearer" in protocols else ""
        sock.sendall((reply + "\r\n").encode())
        sock.settimeout(None)

        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir("/home/box")
                os.execve("/bin/bash", ["bash", "-l"], child_env())
            finally:
                os._exit(127)
        log(f"pty open pid {pid}")
        self.resize(fd, cols, rows)
        stop = threading.Event()
        send_lock = threading.Lock()

        def send(opcode, payload):
            with send_lock:
                ws_send(sock, opcode, payload)

        def pump_out():
            try:
                while not stop.is_set():
                    r, _, _ = select.select([fd], [], [], 0.5)
                    if r:
                        data = os.read(fd, 65536)
                        if not data:
                            break
                        send(2, data)
            except OSError:
                pass
            stop.set()
            try:
                send(8, struct.pack(">H", 1000))
            except OSError:
                pass
            try:
                sock.shutdown(socket.SHUT_RD)
            except OSError:
                pass

        t = threading.Thread(target=pump_out, daemon=True)
        t.start()
        try:
            while not stop.is_set():
                op, data = ws_recv(sock, send)
                if op == 8:
                    break
                if op == 9:
                    send(10, data)
                elif op == 2:
                    os.write(fd, data)
                elif op == 1:
                    try:
                        msg = json.loads(data)
                        if isinstance(msg, dict) and "cols" in msg and "rows" in msg:
                            self.resize(fd, int(msg["cols"]), int(msg["rows"]))
                        elif isinstance(msg, dict) and isinstance(msg.get("input"), str):
                            os.write(fd, msg["input"].encode())
                        else:
                            os.write(fd, data)
                    except (ValueError, TypeError):
                        os.write(fd, data)
        except (ConnectionError, OSError):
            pass
        finally:
            stop.set()
            try:
                # owner-checked: pid is the child returned by this connection's pty.fork().
                os.kill(pid, signal.SIGHUP)
            except OSError:
                pass
            t.join(2)
            try:
                os.close(fd)
                for _ in range(20):
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        break
                    time.sleep(0.1)
                else:
                    # owner-checked: only this connection's unreaped pty.fork() child is terminated.
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            except OSError:
                pass
            log(f"pty closed pid {pid}")

    @staticmethod
    def resize(fd, cols, rows):
        cols = max(10, min(cols, 1000)); rows = max(5, min(rows, 500))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PtyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    os.makedirs(os.path.dirname(HEALTH_FILE), exist_ok=True)
    threading.Thread(target=health_writer, daemon=True).start()
    family = socket.AF_INET6 if ":" in BIND else socket.AF_INET
    Server.address_family = PtyServer.address_family = family
    ex = Server((BIND, EXEC_PORT), ExecHandler)
    pt = PtyServer((BIND, PTY_PORT), PtyHandler)
    threading.Thread(target=pt.serve_forever, daemon=True).start()
    log(f"wingmand {VERSION} listening on {BIND}:{EXEC_PORT} (exec) and {BIND}:{PTY_PORT} (pty)")
    ex.serve_forever()


if __name__ == "__main__":
    main()
