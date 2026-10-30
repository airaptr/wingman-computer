#!/usr/bin/env python3
"""Remove npm's illustrative inline private-key example, not its TLS key option."""
from pathlib import Path

npm = Path("/usr/local/lib/node_modules/npm")
files = [
    npm / "docs/content/using-npm/config.md",
    npm / "docs/output/using-npm/config.html",
    npm / "man/man7/config.7",
    npm / "node_modules/@npmcli/config/lib/definitions/definitions.js",
]
for path in files:
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    sample = [line for line in lines if "PRIVATE KEY" in line and "key=" in line]
    if len(sample) != 1:
        raise SystemExit(f"npm's key documentation changed: {path}")
    path.write_text("".join(line for line in lines if line not in sample))
