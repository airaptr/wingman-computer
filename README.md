# Wingman Computer

A Debian desktop running locally under Apple's `container` CLI, with Docker and Podman fallbacks.
This is **stage A: a local development image**. Nothing in this folder has been published, and
`image.lock` intentionally has no release digest yet. Creating the public repository and publishing
GHCR are separate stage B work.

## Run on this Mac

Install Apple's `container` CLI on macOS 26 or newer, then run from this folder:

```sh
./box.sh build
export WINGMAN_COMPUTER_IMAGE=wingman-computer:dev
./box.sh start
./box.sh password
```

Open the printed noVNC address and paste the password. `password` and `token` copy to the clipboard;
neither command prints a credential. The host UI is never automated or focused by these scripts.

The default size is half the host: 2-8 CPUs and 4-15 GiB RAM. `WINGMAN_CPUS` and `WINGMAN_MEMORY`
override it. On cgroup-v2 runtimes, the desktop and interactive shell inherit CPU affinity matching
their quota. Apple's extra VM-overhead CPU stays outside that session, so tools such as `nproc`
report the actual desktop allocation rather than encouraging an extra worker.
`WINGMAN_RUNTIME=container|docker|podman` selects a runtime; otherwise the first available
one in that order is used. Missing runtimes fail with a plain sentence, not a shell traceback.
Apple builds and starts check the host's public package DNS with a two-second bound. If it is
unavailable, they report a fallback to `1.1.1.1` for this computer only, without changing host or
runtime-wide settings. `WINGMAN_DNS` selects explicit resolver IPs separated by spaces; set it to an
empty string to retain runtime DNS even when the probe fails. Docker/Podman build DNS remains a
builder setting.

| Command | Result |
| --- | --- |
| `build [tag]` | Build locally; the default tag is `wingman-computer:dev` |
| `start`, `stop`, `status` | Start, stop/remove, or inspect only this computer; keep its home volume |
| `health` | Authenticated JSON desktop state and image digest |
| `exec 'command'` | Execute as `box` in the desktop's X11 and D-Bus session |
| `shell` | Open an interactive shell in that session |
| `screenshot [file.png]` | Save the real 1920x1200 X11 screen |
| `snapshot [file.tar]` | Save home plus image identity, excluding known account credentials |
| `restore file.tar --yes` | Replace home from a validated, staged snapshot |
| `install-claude` | Install Claude Code on first use; sign in inside the box |
| `vm-ip`, `secret-dir` | Print the VM address or private host credential directory |
| `password`, `token` | Copy the VNC password or API token to the clipboard |
| `image` / `ref`, `url` | Print the selected image reference or noVNC address |
| `pull` | Fetch the digest selected in `image.lock`, after stage B supplies one |

Use `./box.sh <command>`. The home volume is `wingman-computer-home`; no command deletes it.
The script manages `wingman-computer` (and explicitly named `jdg-` test boxes), not other containers.
Stop/start after changing resource or port settings.

## The desktop

Debian 13 (trixie), user `box` (uid 1000, passwordless sudo), `C.UTF-8`, America/Los_Angeles.
Xvfb supplies a 1920x1200 depth-24 screen and depth-32 ARGB visuals. xfwm4 runs without its own
compositor; picom uses XRender. The supervisor waits for a real compositor selection before starting
the panel and Plank, rather than hiding either component to work around black rectangles.
Screenshots read that composed screen directly with Xlib. They do not use `xwd`'s multi-visual
reconstruction, which turns the transparent ARGB dock into a black band even when VNC displays it
correctly. Xlib capture runs in a short-lived process so a display failure cannot terminate the API.

The locked panel has position `p=10;x=0;y=0`, size 40 and icon-size 24. Existing homes get those
panel settings too. Plank contains Chromium, Thunar, Terminal and LibreOffice launchers.

Included: Chromium, LibreOffice Writer/Calc/Impress, Node.js 24/npm, Python 3.13/pip/venv, uv, gh,
git, jq, ripgrep, tmux, xdotool, wmctrl, poppler-utils, PostgreSQL client, rustc/cargo and PulseAudio.
`wingman-browser` uses `~/chrome-profile` and exposes CDP only at `127.0.0.1:9222` **inside** the box.
The home also has `agent-data`, `wingman-data`, `out` and `reference`.

The deliberate differences from Grok Bot are Chromium instead of Chrome (including on arm64),
Claude Code installed on first use instead of baked in, and no window router (1339) or host gateway
(1340). No xAI service code is included. Audio streaming, host clipboard integration and Wingman app
wiring are not part of this stage.

## Local interfaces and credentials

| Host port | Interface | Authentication |
| --- | --- | --- |
| 6080 | noVNC control | VNC password |
| 6081 | noVNC view-only | VNC password |
| 1337 | `GET /health`, `POST /exec`, `GET /screenshot` | Bearer token |
| 1338 | WebSocket `/pty` | Bearer token on upgrade |

All four host mappings bind to `127.0.0.1`. Override host ports with `WINGMAN_NOVNC_PORT`,
`WINGMAN_VIEW_PORT`, `WINGMAN_EXEC_PORT` and `WINGMAN_PTY_PORT`.

Apple containers also have a directly reachable VM address. Consequently the API requires the token
on that address too; raw VNC ports 5900 and 5901 listen only on the VM's loopback. After authentication,
wingmand rejects non-loopback Host headers with 421 and foreign browser Origins with 403.
Browser clients must be same-origin; no Origin is needed by native HTTP or WebSocket clients.
There are no auth cookies, query-string tokens or permissive CORS responses.

On macOS, secrets live in `~/Library/Application Support/Wingman-Computer/secret/`; on Linux,
`${XDG_CONFIG_HOME:-~/.config}/wingman-computer/secret/`. Directories are 0700, files 0600, and the
folder is mounted read-only. Secrets never enter the container configuration, command arguments or
logs. Host HTTP requests send the Authorization header through curl's stdin configuration pipe.
When booted without that mount, the image mints unique credentials in `/etc/wingman/`, root:box 0640.
No token, VNC password, signed-in account or Claude installation is built into the image.

### Exec, PTY and health

Send `Authorization: Bearer <your token>` in a request header, not a URL.
`POST /exec` accepts `{"cmd":"ls ~","timeout":60}` or a string `argv` array, plus optional `cwd`,
string-valued `env`, `stdin` and `detach`. Responses contain stdout/stderr, exit code, timeout and
truncation flags. Commands inherit DISPLAY, the desktop's D-Bus address and the box user's home.

The PTY is `ws://127.0.0.1:1338/pty?cols=120&rows=32`. Binary frames carry terminal bytes;
text frames may contain `{"cols":90,"rows":28}` or `{"input":"ls\r"}`. Plain text input also works.
Browser WebSocket clients can offer `wingman.bearer` and `wingman.bearer.<token>` subprotocols
instead of the header; the credential-bearing subprotocol is never echoed back.

Health reports `ok`, `screen`, `tz`, `locale`, `user`, `uid`, a `processes` array and `image_digest`,
plus listener and uptime details. The same schema is written every five seconds to
`/run/wingman/desktop-health.json`. A not-yet-ready desktop returns 503, not a success-shaped fallback.
For local Docker tags the image identity is Docker's content-addressed image ID; for Apple containers
and pinned registry references it is the OCI digest.

## Snapshots

A snapshot stops this computer, archives its home with a short-lived helper from the same image, and
restarts it if it was running. The helper reuses the desktop's own container name; it never removes
another container or any volume. `./.wingman-snapshot.json` records the image reference, digest,
version and creation date. Output is written to a partial file and renamed only after completion.

Snapshots omit `.claude.json`, `.credentials.json`, `gh/hosts.yml` and transient Chromium locks.
They still contain your other files and may contain other application sessions: treat archives as
private, not as distributable images.

Restore requires `--yes`. A different image digest also requires `--force`; force does not bypass
archive safety checks. Unsafe paths, external symlinks, device entries, credential files, invalid
metadata and incomplete tars are rejected before replacing home. Files are staged on the home volume,
then swapped with a temporary backup so a failed move can roll back. Absolute links to files within
home are made relative when saved. No unvalidated archive is extracted over a live home.

## Build and future release workflow

`Containerfile` pins the multi-architecture Debian base by digest. Node 24 and uv downloads have pinned
SHA-256 checksums; gh comes from its signed apt repository. Slow package and tool layers precede
desktop source. The build removes npm's illustrative inline key example while retaining its TLS
options and third-party license notices. Generated bytecode, archives, local secrets and test
artifacts are excluded from source/build contexts.

After this folder becomes its own public repository, `.github/workflows/image.yml` will:

1. Build on native `ubuntu-24.04` amd64 and `ubuntu-24.04-arm` arm64 runners and export OCI archives.
2. Load each exact archive and gate image hygiene, toolchain, rendering, browser, API, PTY and auth.
3. Wait for **both** gates before pushing those same archived bytes by digest, without rebuilding.
4. Merge the manifests into a dated tag and `latest`, and attest the index with GitHub build provenance.
5. Open a bot PR changing `image.lock`; only merging that reviewed PR selects an image for new boxes.

All Actions are pinned to commit SHAs, with Dependabot updates. PRs run boot gates without registry
write permissions. A weekly uncached rebuild refreshes Debian security packages. Only the lock job
has repository/PR write permissions; it never commits directly to main.

Local regression checks (standard library only):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 tools/check-desktop.py      # with the development computer running
```

`tools/ci-check.sh` is for an isolated Linux Docker CI runner. The external stage-A judge is not
stored or modified here. Stage B will fill `image.lock` with a real published digest; no release
digest or registry-success claim is fabricated in stage A.

## License

Apache-2.0. Copyright (c) 2026 Wildhaven Homes LLC. See `LICENSE` and `NOTICE`.
