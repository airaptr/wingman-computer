import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64
REF = "ghcr.io/airaptr/wingman-computer@" + DIGEST


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wingman-distribution-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.lock = self.directory / "image.lock"
        self.lock.write_text("image=ghcr.io/airaptr/wingman-computer\ndigest=" + DIGEST + "\n")
        self.env = dict(os.environ, HOME=str(self.directory),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        WINGMAN_COMPUTER_LOCK=str(self.lock),
                        WINGMAN_COMPUTER_NAME="wingman-computer",
                        WINGMAN_RUNTIME="docker",
                        FAKE_STATE=str(self.directory / "attempts"),
                        FAKE_FAILURES="0", FAKE_REF=REF)
        self.env.pop("WINGMAN_COMPUTER_IMAGE", None)
        self.program("docker", """#!/bin/bash
set -euo pipefail
case "$1 ${2:-}" in
    "info ") exit 0 ;;
    "image pull")
        test "$3" = "$FAKE_REF"
        attempts=0
        if [ -f "$FAKE_STATE" ]; then attempts=$(cat "$FAKE_STATE"); fi
        attempts=$((attempts + 1))
        printf '%s' "$attempts" > "$FAKE_STATE"
        test "$attempts" -gt "$FAKE_FAILURES"
        ;;
    "image inspect") exit 0 ;;
    *) exit 2 ;;
esac
""")
        self.program("sleep", "#!/bin/sh\nexit 0\n")
        self.program("df", "#!/bin/sh\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted\\ntest 99999999 0 99999999 0%% /\\n'\n")

    def program(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def box(self, *args):
        return subprocess.run(["bash", str(ROOT / "box.sh"), *args], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_ref_uses_the_locked_index(self):
        result = self.box("ref")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), REF)

    def test_unpinned_lock_is_rejected(self):
        self.lock.write_text("image=ghcr.io/airaptr/wingman-computer\ndigest=\n")
        result = self.box("ref")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No released image is pinned", result.stderr)

    def test_explicit_development_image_still_overrides_the_lock(self):
        self.env["WINGMAN_COMPUTER_IMAGE"] = "wingman-computer:dev"
        result = self.box("ref")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "wingman-computer:dev")

    def test_interrupted_pull_retries_the_same_locked_image(self):
        self.env["FAKE_FAILURES"] = "2"
        result = self.box("pull")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.directory / "attempts").read_text(), "3")
        self.assertEqual(result.stderr.count("retrying"), 2)

    def test_failed_pull_is_never_reported_as_success(self):
        self.env["FAKE_FAILURES"] = "3"
        result = self.box("pull")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.directory / "attempts").read_text(), "3")
        self.assertIn("failed after three attempts", result.stderr)

    def test_apple_status_reports_the_selected_index_not_the_platform_manifest(self):
        self.env["WINGMAN_RUNTIME"] = "container"
        inspected = [{
            "status": {"state": "running"},
            "configuration": {"image": {
                "reference": REF,
                "descriptor": {"digest": "sha256:" + "b" * 64},
            }},
        }]
        self.env["FAKE_INSPECT"] = json.dumps(inspected)
        self.program("uname", "#!/bin/sh\nprintf 'Darwin\\n'\n")
        self.program("sw_vers", "#!/bin/sh\nprintf '26.0\\n'\n")
        self.program("container", """#!/bin/bash
set -euo pipefail
case "$1" in
    system) exit 0 ;;
    inspect) printf '%s\\n' "$FAKE_INSPECT" ;;
    image) printf '%s\\n' "$FAKE_IMAGE_INSPECT" ;;
    volume) exit 0 ;;
    *) exit 2 ;;
esac
""")
        result = self.box("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("image_digest: " + DIGEST, result.stdout)
        self.assertNotIn("sha256:" + "b" * 64, result.stdout)

        self.env["FAKE_IMAGE_INSPECT"] = json.dumps([{
            "configuration": {"descriptor": {"digest": "sha256:" + "b" * 64}},
        }])
        self.env["WINGMAN_DNS"] = ""
        self.program("sysctl", """#!/bin/sh
case "$2" in
    hw.ncpu) printf '8\\n' ;;
    hw.memsize) printf '17179869184\\n' ;;
    *) exit 2 ;;
esac
""")
        self.program("curl", "#!/bin/sh\ncat >/dev/null\nprintf '200'\n")
        result = self.box("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://127.0.0.1:6080/", result.stdout)


if __name__ == "__main__":
    unittest.main()
