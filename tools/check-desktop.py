#!/usr/bin/env python3
"""Exercise a running computer through its public interfaces; never display credentials."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]
EXEC_PORT = int(os.environ.get("WINGMAN_EXEC_PORT", "1337"))
PTY_PORT = int(os.environ.get("WINGMAN_PTY_PORT", "1338"))
BASE = f"http://127.0.0.1:{EXEC_PORT}"
TOKEN = b""
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def box(*args):
    result = subprocess.run(["bash", str(ROOT / "box.sh"), *args], capture_output=True)
    require(result.returncode == 0, "box.sh " + args[0] + " failed")
    return result.stdout


def request(path, body=None, authenticated=True, headers=None, base=BASE):
    values = {"Authorization": "Bearer " + TOKEN.decode()} if authenticated else {}
    values.update(headers or {})
    payload = None if body is None else json.dumps(body).encode()
    if payload is not None:
        values["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=payload, headers=values)
    try:
        with HTTP.open(req, timeout=30) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read()


def command(text):
    code, raw = request("/exec", {"cmd": text, "timeout": 60})
    require(code == 200, "authenticated exec was refused")
    result = json.loads(raw)
    require(result["exit_code"] == 0 and not result["timed_out"], "a desktop-session command failed")
    return result["stdout"]


def check_png(png):
    require(png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 50000, "screenshot is not a real desktop PNG")
    position, compressed, geometry = 8, bytearray(), None
    while position + 12 <= len(png):
        size = struct.unpack_from(">I", png, position)[0]
        kind = png[position + 4:position + 8]
        payload = png[position + 8:position + 8 + size]
        if kind == b"IHDR":
            geometry = struct.unpack(">IIBBBBB", payload)
        if kind == b"IDAT":
            compressed.extend(payload)
        position += size + 12
    require(geometry == (1920, 1200, 8, 2, 0, 0, 0), "screenshot geometry or RGB format is wrong")
    pixels = zlib.decompress(compressed)
    stride = 1920 * 3 + 1
    require(len(pixels) == stride * 1200, "screenshot pixel data is incomplete")
    counts = {"screen": [0, 0], "dock": [0, 0]}
    for y in range(0, 1200, 8):
        require(pixels[y * stride] == 0, "the screenshot encoder returned an unexpected PNG filter")
        for x in range(0, 1920, 8):
            offset = y * stride + 1 + x * 3
            black = max(pixels[offset:offset + 3]) < 8
            counts["screen"][0] += black
            counts["screen"][1] += 1
            if y >= 1080 and 480 <= x <= 1440:
                counts["dock"][0] += black
                counts["dock"][1] += 1
    for area, (black, total) in counts.items():
        require(black / total < 0.05, f"black rendering in the {area} area: {black / total:.3f}")


def receive_exact(peer, length):
    result = bytearray()
    while len(result) < length:
        part = peer.recv(length - len(result))
        require(bool(part), "PTY closed before its response")
        result.extend(part)
    return bytes(result)


def send_frame(peer, opcode, payload):
    mask = secrets.token_bytes(4)
    require(len(payload) < 126, "the PTY test frame is too large")
    peer.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
                 + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))


def open_pty(authenticated=True, host=None, origin=None):
    peer = socket.create_connection(("127.0.0.1", PTY_PORT), timeout=5)
    nonce = base64.b64encode(secrets.token_bytes(16)).decode()
    headers = [
        "GET /pty HTTP/1.1",
        "Host: " + (host or f"127.0.0.1:{PTY_PORT}"),
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Key: " + nonce,
    ]
    if authenticated:
        headers.append("Authorization: Bearer " + TOKEN.decode())
    if origin is not None:
        headers.append("Origin: " + origin)
    peer.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
    reply = bytearray()
    while not reply.endswith(b"\r\n\r\n"):
        reply.extend(receive_exact(peer, 1))
        require(len(reply) < 16384, "PTY upgrade headers are too large")
    code = int(bytes(reply).split(b" ", 2)[1])
    if code == 101:
        expected = base64.b64encode(hashlib.sha1((nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        require(b"Sec-WebSocket-Accept: " + expected in reply, "PTY upgrade accept digest is wrong")
    return peer, code


def check_pty():
    for kwargs, expected in (({"authenticated": False}, 401),
                             ({"host": "foreign.invalid"}, 421),
                             ({"origin": "https://foreign.invalid"}, 403)):
        peer, code = open_pty(**kwargs)
        peer.close()
        require(code == expected, "PTY authentication or browser-origin guard failed")
    peer, code = open_pty()
    with peer:
        require(code == 101, "PTY did not upgrade to a WebSocket")
        send_frame(peer, 1, json.dumps({"cols": 90, "rows": 28}).encode())
        send_frame(peer, 2, b"printf 'wingman-%s\\n' 'pty-ready'\r")
        output = bytearray()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            first, second = receive_exact(peer, 2)
            length = second & 127
            if length == 126:
                length = struct.unpack(">H", receive_exact(peer, 2))[0]
            elif length == 127:
                length = struct.unpack(">Q", receive_exact(peer, 8))[0]
            payload = receive_exact(peer, length)
            if first & 15 in (1, 2):
                output.extend(payload)
            if b"wingman-pty-ready" in output:
                send_frame(peer, 8, struct.pack(">H", 1000))
                return
        raise RuntimeError("PTY did not execute terminal input")


def check_unique_tokens():
    names = [f"jdg-ci-{suffix}-{os.getpid()}" for suffix in ("a", "b")]
    created = []
    try:
        for name in names:
            subprocess.run(["docker", "run", "-d", "--name", name, "--cpus", "2", "--memory", "2g",
                            "wingman-computer:dev"], check=True, stdout=subprocess.DEVNULL)
            created.append(name)
        hashes = []
        for name in names:
            for _ in range(100):
                result = subprocess.run(["docker", "exec", name, "test", "-s", "/etc/wingman/token"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode == 0:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("a fresh box did not create its private token")
            hashes.append(subprocess.check_output(["docker", "exec", name, "sha256sum", "/etc/wingman/token"]))
            permissions = subprocess.check_output(["docker", "exec", name, "stat", "-c", "%U:%G %a",
                                                   "/etc/wingman/token"]).strip()
            require(permissions == b"root:box 640", "guest token ownership is wrong")
        require(hashes[0].split()[0] != hashes[1].split()[0], "two boxes minted the same token")
    finally:
        for name in created:
            # owner-checked: created contains only the exact jdg-ci names successfully started above.
            subprocess.run(["docker", "stop", name], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["docker", "rm", name], check=True, stdout=subprocess.DEVNULL)


def main():
    global TOKEN
    directory = Path(box("secret-dir").decode().strip())
    require(stat.S_IMODE(directory.stat().st_mode) == 0o700, "host secret directory permissions are wrong")
    for name in ("token", "vnc"):
        require(stat.S_IMODE((directory / name).stat().st_mode) == 0o600, "host credential file permissions are wrong")
    TOKEN = (directory / "token").read_bytes().strip()
    require(len(TOKEN) >= 43, "the bearer token has insufficient entropy")
    code, raw = request("/health")
    require(code == 200, "desktop health failed")
    report = json.loads(raw)
    require(report["ok"] and report["screen"] == "1920x1200", "the desktop is not ready")
    require((report["tz"], report["locale"], report["user"], report["uid"]) ==
            ("America/Los_Angeles", "C.UTF-8", "box", 1000), "desktop identity or locale differs")
    required = {"Xvfb", "xfwm4", "xfce4-panel", "plank", "picom", "x11vnc", "websockify"}
    require(required <= set(report["processes"]), "a desktop component is missing")
    require(report["image_digest"].startswith("sha256:"), "health is missing its image identity")

    for path, body in (("/health", None), ("/screenshot", None), ("/exec", {"cmd": "id"})):
        require(request(path, body, authenticated=False)[0] == 401, "an API route is missing authentication")
    require(request("/health", headers={"Host": "foreign.invalid"})[0] == 421, "Host guard failed")
    require(request("/health", headers={"Origin": "https://foreign.invalid"})[0] == 403, "Origin guard failed")
    vm_ip = box("vm-ip").decode().strip()
    require(request("/health", authenticated=False, base=f"http://{vm_ip}:1337")[0] == 401,
            "VM-address API access is missing authentication")
    for port in (5900, 5901):
        try:
            connection = socket.create_connection((vm_ip, port), timeout=1)
        except (ConnectionRefusedError, TimeoutError):
            continue
        connection.close()
        raise RuntimeError("raw VNC is reachable on the VM address")

    output = command("id -un; id -u; printf '%s\\n' \"$LANG\"; cat /etc/timezone; sudo -n true")
    require(output.splitlines() == ["box", "1000", "C.UTF-8", "America/Los_Angeles"],
            "exec is not using the box desktop session")
    panel = command("for key in position size icon-size; do "
                    "xfconf-query -c xfce4-panel -p /panels/panel-1/$key; done")
    require(panel.splitlines() == ["p=10;x=0;y=0", "40", "24"], "the panel does not match the desktop contract")
    require("xfwm4" in command("wmctrl -m").lower(), "the window manager is not xfwm4")
    command("node -v | grep -q '^v24\\.' && python3 -V | grep -q '3\\.13\\.' && "
            "for tool in chromium soffice gh uv git jq rg tmux xdotool wmctrl rustc cargo "
            "thunar xfce4-terminal plank picom hsetroot; do command -v \"$tool\" >/dev/null || exit 1; done")
    for port in (6080, 6081):
        with HTTP.open(f"http://127.0.0.1:{port}/vnc.html", timeout=5) as response:
            require(response.status == 200, "noVNC assets are unavailable")
    code, png = request("/screenshot")
    require(code == 200, "the screenshot API failed")
    check_png(png)
    check_pty()
    command("setsid -f wingman-browser about:blank >/dev/null 2>&1; "
            "for attempt in $(seq 30); do "
            "curl -fs http://127.0.0.1:9222/json/version >/dev/null && exit 0; sleep 0.2; done; exit 1")
    require("chromium" in command("wmctrl -l").lower(), "Chromium has no desktop window")
    guest_state = command("ps -eo args; cat /var/log/wingman/*.log 2>/dev/null").encode()
    runtime = os.environ.get("WINGMAN_RUNTIME", "container")
    host_state = (subprocess.check_output([runtime, "inspect", "wingman-computer"])
                  + subprocess.check_output([runtime, "logs", "wingman-computer"], stderr=subprocess.STDOUT)
                  + subprocess.check_output(["ps", "-axww", "-o", "args"]))
    for name in ("token", "vnc"):
        value = (directory / name).read_bytes().strip()
        require(value not in host_state and value not in guest_state, "a credential appeared in process state or logs")
    if runtime == "docker":
        check_unique_tokens()
    print("desktop, rendering, browser, HTTP, PTY and credential gates passed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit("desktop check failed: " + str(error))
