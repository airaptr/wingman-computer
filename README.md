# Wingman Computer

A Linux desktop in a box that you run on your own machine and open in a browser.
One public image, pinned by digest, so any Wingman with internet access can pull the same computer
and boot it in seconds.

- **Debian 13 (trixie)**, for `linux/arm64` (Apple silicon Macs) and `linux/amd64`.
- **Screen** 1920x1200, `C.UTF-8`, time zone America/Los_Angeles. User `box` (uid 1000), passwordless sudo.
- **Desktop**: xfwm4, one locked top panel (40 px, 24 px icons), Plank dock, picom, a gradient wallpaper.
- **Apps**: Chromium (`wingman-browser`, profile in `~/chrome-profile`), Thunar, Xfce Terminal,
  LibreOffice Writer, Calc and Impress.
- **Tools**: Node.js 24 and npm, Python 3.13 with pip and venv, uv, git, gh, jq, ripgrep, tmux, xdotool,
  wmctrl, poppler-utils, postgresql-client, rustc and cargo, openssh-client, PulseAudio.
- **Home** (`/home/box`, a volume that survives restarts): `agent-data`, `wingman-data`, `chrome-profile`,
  `out`, `reference`, `cli-config`, `.claude`.

Nothing is signed in and no account or license is baked into the image. Claude Code and `gh` are set up
by you, inside your box, the first time you need them.

## Start it

```sh
git clone https://github.com/airaptr/wingman-computer && cd wingman-computer
./box.sh start       # pulls the image pinned in image.lock, starts it, prints the address
./box.sh password    # copies the VNC password to the clipboard
```

Open `http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale` and paste the password.

On a Mac this uses Apple's `container` CLI (`brew install container`). Elsewhere it uses Docker or Podman.
`WINGMAN_CPUS` (4), `WINGMAN_MEMORY` (6G) and the `WINGMAN_*_PORT` variables change size and ports.

| Command | What it does |
| --- | --- |
| `box.sh pull` | Fetch the image pinned in `image.lock`, by digest |
| `box.sh start` / `stop` / `status` | Run the box, stop it (home stays), see its state |
| `box.sh password` / `token` | Copy the VNC password or the box token to the clipboard |
| `box.sh health` | The desktop health report |
| `box.sh screenshot [file.png]` | Save the screen |
| `box.sh exec 'command'` | Run a command as `box` and print its output |
| `box.sh shell` | A shell inside the box |
| `box.sh install-claude` | Install Claude Code inside the box with Anthropic's official installer |
| `box.sh snapshot [file.tar]` | Save the home folder to a tar on your machine |
| `box.sh restore file.tar` | Replace the home folder with a snapshot |
| `box.sh build [tag]` | Build the image yourself from `Containerfile` |

## Ports and access

Everything is published on `127.0.0.1` only, so only your own machine can reach it.

| Port | Service | Access |
| --- | --- | --- |
| 6080 | noVNC, full control | VNC password |
| 6081 | noVNC, view only | VNC password |
| 1337 | exec: `POST /exec`, `GET /health`, `GET /screenshot` | box token |
| 1338 | pty: WebSocket terminal at `/pty` | box token |

The box token and the VNC password are made the first time you start the box. They are kept on your
machine in `~/Library/Application Support/Wingman-Computer/secret/` (Linux: `~/.config/wingman-computer/secret/`),
mode 0600, mounted read-only into the box, and never printed, logged or put in the image.
If you run the image without `box.sh`, the box makes its own at first boot in `/etc/wingman/` (readable by root and `box`).

Inside the box the services listen on `127.0.0.1` unless `WINGMAN_BIND` is set. `box.sh` sets
`WINGMAN_BIND=0.0.0.0` because container port publishing connects to the box's own network address,
not its loopback; the host side of every port is still `127.0.0.1`.

### exec

```sh
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:1337/exec \
     -d '{"cmd": "ls ~", "timeout": 60}'
# {"exit_code": 0, "stdout": "...", "stderr": "", "timed_out": false, ...}
```

Send `cmd` (run by `bash -lc`) or `argv`, with optional `cwd`, `env`, `stdin`, `timeout` (seconds, at most 3600).
`"detach": true` starts a window or other long runner and returns at once.

### pty

A WebSocket at `ws://127.0.0.1:1338/pty?cols=120&rows=32`. Binary frames carry terminal bytes both ways;
a text frame `{"cols": N, "rows": N}` resizes. Send the token as `Authorization: Bearer <token>`, or from a
browser as the subprotocols `wingman.bearer` and `wingman.bearer.<token>`.

### health

`GET /health` returns the same report the box writes every 5 seconds to `/run/wingman/desktop-health.json`:
`ok`, the screen size, which desktop parts are running and which ports are listening.

## Lock in the snapshot

- **The image** is pinned by digest in `image.lock`. CI writes it after each build that passes its boot
  test. `box.sh pull` and `start` use that digest, never `latest`, so every machine gets the same bytes.
  Tags `YYYY.MM.DD` and `latest` exist for people browsing the registry.
- **Your files** live in the home volume. `box.sh snapshot` saves it to a tar, `box.sh restore` puts it back,
  on this machine or another one.

Check where an image came from:

```sh
gh attestation verify oci://ghcr.io/airaptr/wingman-computer@<digest> --owner airaptr
```

## How it is built

`Containerfile` starts from `debian:trixie` pinned by digest, installs Debian packages, `gh` from GitHub's
signed apt repository, and Node.js and uv from their release files checked against pinned SHA-256 sums.
`rootfs/` holds the supervisor (`entrypoint.sh`), the box services (`wingmand.py`, Python standard library
only), the panel, dock and theme settings, and the browser wrapper.

`.github/workflows/image.yml` builds each architecture on a native runner, boots it and checks `/health`
(and that a request without the token gets 401), joins both into one multi-arch image, attests its build
provenance, and commits the new digest to `image.lock`.

## Not included yet

- A window router and a host gateway service.
- Audio over noVNC (PulseAudio runs, nothing streams it out).

## License

Apache License 2.0. Copyright (c) 2026 Wildhaven Homes LLC. See `LICENSE` and `NOTICE`.
