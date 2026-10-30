import ctypes
import importlib.util
import io
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import tarfile
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch
import zlib

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "rootfs/opt/wingman" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot = load("snapshot", "snapshot.py")
cpu_session = load("cpu_session", "cpu-session.py")
screen_capture = load("screen_capture", "screenshot.py")
with tempfile.TemporaryDirectory(prefix="wingman-test-token-") as directory:
    token_file = Path(directory) / "token"
    token_file.write_text(secrets.token_urlsafe(32))
    with patch.dict(os.environ, {"WINGMAN_TOKEN_FILE": str(token_file)}):
        wingmand = load("wingmand", "wingmand.py")


class CPUQuotaTests(unittest.TestCase):
    def test_guest_overhead_cpu_is_not_part_of_the_session(self):
        self.assertEqual(cpu_session.quota_affinity(range(9), "800000 100000"),
                         set(range(8)))

    def test_existing_affinity_is_respected(self):
        self.assertEqual(cpu_session.quota_affinity({2, 4, 6}, "200000 100000"), {2, 4})
        self.assertEqual(cpu_session.quota_affinity({2, 4}, "800000 100000"), {2, 4})

    def test_fractional_quota_keeps_enough_cpus(self):
        self.assertEqual(cpu_session.quota_affinity(range(9), "150000 100000"), {0, 1})

    def test_unlimited_quota_keeps_existing_affinity(self):
        self.assertEqual(cpu_session.quota_affinity({1, 3, 5}, "max 100000"), {1, 3, 5})

    def test_invalid_quota_is_not_silently_ignored(self):
        for value in ("", "max", "0 100000", "-1 100000", "100000 0", "bad 100000"):
            with self.assertRaises(ValueError):
                cpu_session.quota_affinity(range(9), value)


class ScreenCaptureTests(unittest.TestCase):
    def pixel_data(self, png):
        position, compressed = 8, bytearray()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        while position < len(png):
            length = struct.unpack_from(">I", png, position)[0]
            if png[position + 4:position + 8] == b"IDAT":
                compressed.extend(png[position + 8:position + 8 + length])
            position += length + 12
        return zlib.decompress(compressed)

    def test_composed_pixels_keep_color_and_ignore_row_padding(self):
        for bpp, order, data in (
                (32, 0, bytes.fromhex("332211ff 66554400 deadbeef")),
                (32, 1, bytes.fromhex("ff112233 00445566 deadbeef")),
                (24, 0, bytes.fromhex("332211 665544 dead")),
                (24, 1, bytes.fromhex("112233 445566 dead"))):
            png = screen_capture.encode_png(2, 1, bpp, len(data), order, data)
            self.assertEqual(struct.unpack_from(">II", png, 16), (2, 1))
            self.assertEqual(self.pixel_data(png), bytes.fromhex("00 112233 445566"))

    def test_invalid_pixel_data_is_rejected(self):
        for arguments in ((0, 1, 32, 4, 0, b"\x00" * 4),
                          (1, 1, 16, 2, 0, b"\x00" * 2),
                          (1, 1, 32, 4, 2, b"\x00" * 4),
                          (2, 1, 32, 4, 0, b"\x00" * 4),
                          (1, 1, 32, 4, 0, b"")):
            with self.assertRaises(ValueError):
                screen_capture.encode_png(*arguments)

    def test_capture_reads_the_composed_root_and_releases_xlib_resources(self):
        pixels = ctypes.create_string_buffer(bytes.fromhex("4f321aff"))
        image = screen_capture.XImage(
            width=1, height=1, format=2, data=ctypes.addressof(pixels), byte_order=0,
            depth=24, bytes_per_line=4, bits_per_pixel=32,
            red_mask=0xFF0000, green_mask=0xFF00, blue_mask=0xFF)
        library = types.SimpleNamespace(**{
            name: Mock(return_value=value) for name, value in {
                "XOpenDisplay": 1, "XDefaultScreen": 0, "XDefaultRootWindow": 2,
                "XDisplayWidth": 1, "XDisplayHeight": 1, "XGetImage": ctypes.pointer(image),
                "XDestroyImage": 0, "XCloseDisplay": 0,
            }.items()
        })
        with patch.object(screen_capture.ctypes, "CDLL", return_value=library):
            captured = screen_capture.read_screen(":1")
        self.assertEqual(captured, (1, 1, 32, 4, 0, bytes.fromhex("4f321aff")))
        library.XGetImage.assert_called_once_with(1, 2, 0, 0, 1, 1, ctypes.c_ulong(-1).value, 2)
        library.XDestroyImage.assert_called_once()
        library.XCloseDisplay.assert_called_once_with(1)

    def test_screenshot_isolates_xlib_failures_from_the_http_server(self):
        with patch.object(wingmand.subprocess, "run",
                          return_value=types.SimpleNamespace(stdout=b"composed PNG")) as run:
            self.assertEqual(wingmand.screenshot_png(), b"composed PNG")
        run.assert_called_once_with(
            ["python3", "-I", "/opt/wingman/screenshot.py", wingmand.DISPLAY],
            capture_output=True, timeout=20, check=True)

    def test_failed_xlib_read_closes_the_display(self):
        library = Mock()
        library.XOpenDisplay.return_value = 1
        library.XDefaultScreen.return_value = 0
        library.XDisplayWidth.return_value = 1920
        library.XDisplayHeight.return_value = 1200
        library.XGetImage.return_value = None
        with patch.object(screen_capture.ctypes, "CDLL", return_value=library):
            with self.assertRaises(OSError):
                screen_capture.read_screen(":1")
        library.XDestroyImage.assert_not_called()
        library.XCloseDisplay.assert_called_once_with(1)


class ServiceTests(unittest.TestCase):
    def test_loopback_authorities(self):
        for value in ("127.0.0.1:1337", "localhost:1337", "[::1]:1337"):
            self.assertIsNotNone(wingmand.loopback_authority(value))
        for value in (None, "", "evil.example", "192.168.64.2:1337",
                      "evil@127.0.0.1", "127.0.0.1/evil", "127.0.0.1:bad",
                      "127.0.0.1:99999", "127.0.0.1?evil", "127.0.0.1\\evil"):
            self.assertIsNone(wingmand.loopback_authority(value), value)

    def test_origin_guard(self):
        self.assertIsNone(wingmand.request_guard("127.0.0.1:1337", None))
        self.assertIsNone(wingmand.request_guard("127.0.0.1:1337", "http://127.0.0.1:1337"))
        for origin in ("null", "https://evil.example", "http://127.0.0.1:9999",
                       "http://127.0.0.1:1337/path", "http://evil@127.0.0.1:1337"):
            self.assertEqual(wingmand.request_guard("127.0.0.1:1337", origin)[0], 403)
        self.assertEqual(wingmand.request_guard("evil.example", None)[0], 421)

    def test_health_contract(self):
        with patch.object(wingmand, "running_comms", return_value=wingmand.PROCESSES), \
             patch.object(wingmand, "listening_ports", return_value=set(wingmand.PORTS.values())), \
             patch.object(wingmand, "screen_size", return_value="1920x1200"), \
             patch.object(wingmand.os, "getuid", return_value=1000), \
             patch.object(wingmand.pwd, "getpwuid", return_value=types.SimpleNamespace(pw_name="box")), \
             patch.dict(os.environ, {"WINGMAN_IMAGE_DIGEST": "sha256:" + "a" * 64,
                                     "LANG": "C.UTF-8", "TZ": "America/Los_Angeles"}):
            report = wingmand.health()
        self.assertTrue(report["ok"])
        self.assertIsInstance(report["processes"], list)
        self.assertEqual(report["uid"], 1000)
        self.assertEqual(report["user"], "box")
        self.assertEqual(report["locale"], "C.UTF-8")
        self.assertEqual(report["tz"], "America/Los_Angeles")
        self.assertEqual(report["image_digest"], "sha256:" + "a" * 64)

    def test_exec_input_validation(self):
        for request in ({"cmd": "true", "timeout": 0}, {"cmd": "true", "timeout": float("nan")},
                        {"cmd": "true", "timeout": float("inf")}, {"argv": []},
                        {"cmd": "true", "env": {"A": 1}}, {"cmd": "true", "env": []},
                        {"cmd": "true", "cwd": 0}, {"cmd": "true", "stdin": 12},
                        {"cmd": "true", "detach": "false"}):
            with self.assertRaises((ValueError, TypeError)):
                wingmand.run_command(request)

    def test_http_auth_before_host_guard(self):
        with patch.object(wingmand, "health", return_value={"ok": True}), \
             patch.object(wingmand, "log"):
            server = wingmand.Server(("127.0.0.1", 0), wingmand.ExecHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                address = "http://127.0.0.1:%s/health" % server.server_port
                cases = [
                    ({}, 401),
                    ({"Authorization": "Bearer " + wingmand.TOKEN.decode()}, 200),
                    ({"Authorization": "Bearer " + wingmand.TOKEN.decode(), "Host": "evil.example"}, 421),
                    ({"Authorization": "Bearer " + wingmand.TOKEN.decode(), "Origin": "https://evil.example"}, 403),
                    ({"Host": "192.168.64.2"}, 401),
                ]
                for headers, expected in cases:
                    request = urllib.request.Request(address, headers=headers)
                    try:
                        with urllib.request.urlopen(request, timeout=2) as response:
                            actual = response.status
                    except urllib.error.HTTPError as error:
                        actual = error.code
                        error.close()
                    self.assertEqual(actual, expected)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_fragmented_websocket_with_ping(self):
        def frame(opcode, payload, final=True):
            mask = secrets.token_bytes(4)
            return (bytes([(0x80 if final else 0) | opcode, 0x80 | len(payload)]) + mask
                    + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

        client, server = socket.socketpair()
        with client, server:
            client.sendall(frame(1, b"echo ", False) + frame(9, b"ping")
                           + frame(0, b"desktop\r"))
            self.assertEqual(wingmand.ws_recv(server), (1, b"echo desktop\r"))
            self.assertEqual(client.recv(64), b"\x8a\x04ping")

    def test_missing_runtime_is_one_plain_sentence(self):
        env = dict(os.environ, PATH="/usr/bin:/bin", WINGMAN_RUNTIME="nonexistent")
        result = subprocess.run(["bash", str(ROOT / "box.sh"), "start"],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertNotIn("command not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_optional_flags_on_macos_system_bash(self):
        result = subprocess.run(["bash", "-uc",
                                 'flags=(); set -- ${flags[@]+"${flags[@]}"}; test "$#" -eq 0'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wingman-archive-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "out").mkdir()
        (self.home / "out/note").write_text("keep this")
        self.archive = self.root / "home.tar"
        self.digest = "sha256:" + "a" * 64

    def save(self):
        snapshot.save_archive(self.archive, self.home, "wingman-computer:dev", self.digest)

    def restore(self, digest=None, force=False):
        snapshot.restore_archive(self.archive, self.home, digest or self.digest, force,
                                 uid=os.getuid(), gid=os.getgid())

    def test_roundtrip_and_metadata(self):
        self.save()
        with tarfile.open(self.archive) as archive:
            metadata = snapshot.read_metadata(archive)
        self.assertEqual(metadata["image"], "wingman-computer:dev")
        self.assertEqual(metadata["image_digest"], self.digest)
        (self.home / "out/note").unlink()
        (self.home / "obsolete").write_text("remove this")
        self.restore()
        self.assertEqual((self.home / "out/note").read_text(), "keep this")
        self.assertFalse((self.home / "obsolete").exists())
        self.assertFalse(any(p.name.startswith(snapshot.RESERVED) for p in self.home.iterdir()))

    def test_credentials_and_runtime_locks_excluded(self):
        for name in (".claude.json", ".claude/.credentials.json", ".config/gh/hosts.yml",
                     "chrome-profile/SingletonLock"):
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test data")
        self.save()
        with tarfile.open(self.archive) as archive:
            names = archive.getnames()
        for name in names:
            if snapshot.member_path(name) != snapshot.METADATA:
                self.assertFalse(snapshot.excluded(name))

    def test_digest_mismatch_keeps_home(self):
        self.save()
        (self.home / "out/note").write_text("current")
        with self.assertRaisesRegex(ValueError, "different image digest"):
            self.restore("sha256:" + "b" * 64)
        self.assertEqual((self.home / "out/note").read_text(), "current")
        self.restore("sha256:" + "b" * 64, force=True)
        self.assertEqual((self.home / "out/note").read_text(), "keep this")

    def test_absolute_home_symlink_becomes_portable(self):
        (self.home / "link").symlink_to(self.home / "out/note")
        self.save()
        self.restore()
        self.assertEqual(os.readlink(self.home / "link"), "out/note")
        self.assertEqual((self.home / "link").read_text(), "keep this")

    def test_traversal_and_external_symlinks_refused(self):
        for path in ("/etc/passwd", "../outside", "./../../outside", ".wingman-restore-fake/file"):
            with self.assertRaises(ValueError):
                snapshot.member_path(path)
        (self.home / "outside").symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            self.save()
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.archive.with_name("home.tar.part").exists())

    def test_malicious_archive_leaves_home_intact(self):
        self.save()
        with tarfile.open(self.archive, "a") as archive:
            info = tarfile.TarInfo("../outside")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bad"))
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual((self.home / "out/note").read_text(), "keep this")
        self.assertFalse((self.root / "outside").exists())

    def test_truncated_archive_keeps_home(self):
        self.archive.write_bytes(b"not a tar")
        with self.assertRaises(tarfile.TarError):
            self.restore()
        self.assertEqual((self.home / "out/note").read_text(), "keep this")


if __name__ == "__main__":
    unittest.main()
