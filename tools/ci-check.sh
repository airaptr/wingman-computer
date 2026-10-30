#!/bin/bash
# Run on an isolated native Linux CI runner, never against a user's desktop data.
set -euo pipefail
cd "$(dirname "$0")/.."
export WINGMAN_RUNTIME=docker
export WINGMAN_COMPUTER_IMAGE=wingman-computer:dev

cleanup() {
    bash box.sh stop >/dev/null
}
trap cleanup EXIT
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
test -z "$(git ls-files . | grep -E '(__pycache__|\.pyc$|\.env$|\.tar$)' || true)"

docker run --rm --name jdg-ci-hygiene --entrypoint python3 "$WINGMAN_COMPUTER_IMAGE" -c '
from pathlib import Path
import re
for name in ("/etc/wingman/token", "/etc/wingman/vnc",
             "/etc/ssl/private/ssl-cert-snakeoil.key", "/home/box/.claude.json",
             "/home/box/.claude/.credentials.json", "/home/box/.config/gh/hosts.yml"):
    assert not Path(name).exists(), "unexpected credential file in the image"
pattern = re.compile(rb"(?:gh[po]_|github[_]pat_|sk[-]ant-|xai-)[A-Za-z0-9_]{10,}|BEGIN [A-Z ]*PRIVATE KEY")
for root in ("/etc", "/home", "/opt", "/root", "/usr/local"):
    for path in Path(root).rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                tail = b""
                while chunk := stream.read(1024 * 1024):
                    data = tail + chunk
                    assert not pattern.search(data), "credential-like material in image file: " + str(path)
                    tail = data[-256:]
print("image hygiene passed")
'
bash box.sh start
python3 tools/check-desktop.py
