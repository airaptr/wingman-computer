#!/bin/bash
# A local desktop with a persistent home; credentials travel only through files and private pipes.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
NAME=${WINGMAN_COMPUTER_NAME:-wingman-computer}
VOLUME=${WINGMAN_COMPUTER_VOLUME:-$NAME-home}
LOCK=${WINGMAN_COMPUTER_LOCK:-$HERE/image.lock}
P_NOVNC=${WINGMAN_NOVNC_PORT:-6080}
P_VIEW=${WINGMAN_VIEW_PORT:-6081}
P_EXEC=${WINGMAN_EXEC_PORT:-1337}
P_PTY=${WINGMAN_PTY_PORT:-1338}
if [ "$(uname -s)" = Darwin ]; then
    SUPPORT="$HOME/Library/Application Support/Wingman-Computer"
else
    SUPPORT="${XDG_CONFIG_HOME:-$HOME/.config}/wingman-computer"
fi
[ "$NAME" = wingman-computer ] || SUPPORT="$SUPPORT/boxes/$NAME"
SECRET_DIR="$SUPPORT/secret"
RT=${WINGMAN_RUNTIME:-}

die() { printf '%s\n' "$*" >&2; exit 1; }

if [ "$NAME" != wingman-computer ] && ! [[ "$NAME" =~ ^jdg-[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    die "This script manages only wingman-computer or a jdg- test box."
fi

preflight() {
    if [ -z "$RT" ]; then
        local candidate
        for candidate in container docker podman; do
            if command -v "$candidate" >/dev/null 2>&1; then RT=$candidate; break; fi
        done
    fi
    [ -n "$RT" ] && command -v "$RT" >/dev/null 2>&1 \
        || die "The container runtime is missing; install Apple's container CLI, Docker or Podman."
    case "$RT" in
        container|docker|podman) ;;
        *) die "The selected runtime is unsupported; use container, docker or podman." ;;
    esac
    command -v python3 >/dev/null 2>&1 || die "Python 3 is missing; install it before starting the computer."
    command -v curl >/dev/null 2>&1 || die "curl is missing; install it before starting the computer."
    if [ "$RT" = container ]; then
        [ "$(uname -s)" = Darwin ] || die "Apple's container CLI needs a Mac; use Docker or Podman here."
        local version
        version=$(sw_vers -productVersion)
        [ "${version%%.*}" -ge 26 ] || die "Apple's container CLI needs macOS 26 or newer; update macOS first."
        if ! container system status >/dev/null 2>&1; then
            container system start >/dev/null 2>&1 \
                || die "The container service could not start; run container system start to repair it."
        fi
    else
        "$RT" info >/dev/null 2>&1 \
            || die "The container service is not running; start $RT and try again."
    fi
}

image_ref() {
    if [ -n "${WINGMAN_COMPUTER_IMAGE:-}" ]; then printf '%s\n' "$WINGMAN_COMPUTER_IMAGE"; return; fi
    [ -f "$LOCK" ] || die "The image lock is missing; restore image.lock or set WINGMAN_COMPUTER_IMAGE."
    local image digest
    image=$(sed -n 's/^image=//p' "$LOCK")
    digest=$(sed -n 's/^digest=//p' "$LOCK")
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] && [ -n "$image" ] \
        || die "No released image is pinned yet; build locally and set WINGMAN_COMPUTER_IMAGE=wingman-computer:dev."
    printf '%s@%s\n' "$image" "$digest"
}

have_image() { "$RT" image inspect "$1" >/dev/null 2>&1; }

image_digest() {
    if [ "$RT" = container ]; then
        container image inspect "$1" | python3 -c \
            'import json,sys; print(json.load(sys.stdin)[0]["configuration"]["descriptor"]["digest"])'
    elif [[ "$1" == *@sha256:* ]]; then
        printf '%s\n' "${1##*@}"
    else
        "$RT" image inspect "$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["Id"])'
    fi
}

pull() {
    preflight
    local ref
    ref=$(image_ref)
    local free
    free=$(df -Pk "$HERE" | awk 'END {print $4}')
    [ "$free" -ge 4194304 ] || die "The image needs more free disk space; free at least 4 GiB and try again."
    "$RT" image pull "$ref"
    have_image "$ref" || die "The image download did not finish; check your connection and try again."
}

ensure_secrets() {
    python3 - "$SUPPORT" "$SECRET_DIR" <<'PY'
import os
from pathlib import Path
import secrets
import stat
import string
import sys

try:
    for path in map(Path, sys.argv[1:]):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        status = path.lstat()
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise ValueError("the secret directory must be owned by this user and cannot be a symlink")
        path.chmod(0o700)
    directory = Path(sys.argv[2])
    for name, length in (("vnc", 8), ("token", 43)):
        path = directory / name
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            status = path.lstat()
            if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid():
                raise ValueError("a secret file has unsafe ownership or is not a regular file")
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write("".join(secrets.choice(string.ascii_letters + string.digits)
                                     for _ in range(length)).encode())
        path.chmod(0o600)
        value = path.read_bytes()
        if len(value) != length or not all(chr(c) in string.ascii_letters + string.digits + "-_" for c in value):
            raise ValueError("a secret file has invalid contents")
except (OSError, ValueError):
    sys.exit("The local secret files are unsafe or unreadable; check permissions in Wingman-Computer.")
PY
}

state() {
    local info
    if ! info=$("$RT" inspect "$NAME" 2>/dev/null); then printf 'absent\n'; return; fi
    if [ "$RT" = container ]; then
        printf '%s' "$info" | python3 -c \
            'import json,sys; print(json.load(sys.stdin)[0]["status"]["state"])'
    else
        printf '%s' "$info" | python3 -c \
            'import json,sys; print(json.load(sys.stdin)[0]["State"]["Status"])'
    fi
}

running_image() {
    if [ "$RT" = container ]; then
        container inspect "$NAME" | python3 -c \
            'import json,sys; print(json.load(sys.stdin)[0]["configuration"]["image"]["reference"])'
    else
        "$RT" inspect "$NAME" | python3 -c \
            'import json,sys; print(json.load(sys.stdin)[0]["Config"]["Image"])'
    fi
}

running_digest() {
    if [ "$RT" = container ]; then
        container inspect "$NAME" | python3 -c \
            'import json,sys; print(json.load(sys.stdin)[0]["configuration"]["image"]["descriptor"]["digest"])'
    else
        "$RT" inspect "$NAME" | python3 -c 'import json,sys
d=json.load(sys.stdin)[0]
print(next((e.split("=",1)[1] for e in d["Config"]["Env"] if e.startswith("WINGMAN_IMAGE_DIGEST=")), d["Image"]))'
    fi
}

resources() {
    local cpus bytes gib
    if [ "$(uname -s)" = Darwin ]; then
        cpus=$(sysctl -n hw.ncpu)
        bytes=$(sysctl -n hw.memsize)
    else
        cpus=$(getconf _NPROCESSORS_ONLN)
        bytes=$(awk '/^MemTotal:/ {printf "%.0f", $2 * 1024}' /proc/meminfo)
    fi
    cpus=$((cpus / 2))
    [ "$cpus" -ge 2 ] || cpus=2
    [ "$cpus" -le 8 ] || cpus=8
    gib=$((bytes / 2 / 1073741824))
    [ "$gib" -ge 4 ] || gib=4
    [ "$gib" -le 15 ] || gib=15
    CPUS=${WINGMAN_CPUS:-$cpus}
    MEMORY=${WINGMAN_MEMORY:-${gib}G}
    [[ "$CPUS" =~ ^[1-9][0-9]*$ ]] || die "WINGMAN_CPUS must be a positive whole number."
    [[ "$MEMORY" =~ ^[1-9][0-9]*[kKmMgG]?$ ]] || die "WINGMAN_MEMORY must be a positive size such as 8G."
}

dns_options() {
    DNS_ARGS=()
    local servers=${WINGMAN_DNS:-}
    if [ "${WINGMAN_DNS+x}" != x ] && [ "$RT" = container ]; then
        if ! python3 - <<'PY'
import socket
import sys
import threading

resolved = []
def probe():
    try:
        socket.getaddrinfo("cli.github.com", 443)
        resolved.append(True)
    except OSError:
        pass
thread = threading.Thread(target=probe, daemon=True)
thread.start()
thread.join(2)
sys.exit(0 if resolved else 1)
PY
        then
            servers=1.1.1.1
            printf '%s\n' "The host resolver is unavailable; using 1.1.1.1 for this computer only." >&2
        fi
    fi
    if [ -n "$servers" ]; then
        local server
        for server in $servers; do
            python3 -c 'import ipaddress,sys; ipaddress.ip_address(sys.argv[1])' "$server" 2>/dev/null \
                || die "WINGMAN_DNS must contain DNS server IP addresses separated by spaces."
            DNS_ARGS+=(--dns "$server")
        done
    fi
}

curl_auth() {
    [ -s "$SECRET_DIR/token" ] || die "The box token is missing; run box.sh start first."
    local token
    token=$(<"$SECRET_DIR/token")
    [[ "$token" =~ ^[A-Za-z0-9_-]{43}$ ]] || die "The box token file is invalid; repair the local secret files."
    printf 'header = "Authorization: Bearer %s"\n' "$token" \
        | curl --config - --noproxy '*' --silent --show-error --fail-with-body --connect-timeout 2 "$@"
}

url() { printf 'http://127.0.0.1:%s/vnc.html?autoconnect=1&resize=scale\n' "$P_NOVNC"; }

stop() {
    preflight
    local current
    current=$(state)
    if [ "$current" != absent ]; then
        if [ "$current" = running ]; then "$RT" stop "$NAME" >/dev/null; fi
        # owner-checked: preflight restricts NAME to this desktop or a jdg- test box; no volume is removed.
        "$RT" rm "$NAME" >/dev/null
    fi
    printf 'stopped\n'
}

start() {
    preflight
    ensure_secrets
    resources
    dns_options
    local ref digest current
    ref=$(image_ref)
    have_image "$ref" || pull
    digest=$(image_digest "$ref")
    "$RT" volume inspect "$VOLUME" >/dev/null 2>&1 || "$RT" volume create "$VOLUME" >/dev/null
    current=$(state)
    if [ "$current" != absent ] && [ "$(running_digest)" != "$digest" ]; then
        stop >/dev/null
        current=absent
    fi
    case "$current" in
        running) ;;
        absent)
            local mount=() size=() progress=()
            if [ "$RT" = container ]; then
                mount=(--mount "type=bind,source=$SECRET_DIR,target=/run/wingman-secret,readonly")
                size=(-c "$CPUS" -m "$MEMORY" --shm-size 1g)
                progress=(--progress none)
            else
                mount=(-v "$SECRET_DIR:/run/wingman-secret:ro")
                size=(--cpus "$CPUS" --memory "$MEMORY" --shm-size 1g)
            fi
            "$RT" run -d --name "$NAME" "${size[@]}" ${progress[@]+"${progress[@]}"} \
                ${DNS_ARGS[@]+"${DNS_ARGS[@]}"} \
                -e WINGMAN_BIND=0.0.0.0 -e "WINGMAN_IMAGE_DIGEST=$digest" \
                -p "127.0.0.1:$P_NOVNC:6080" -p "127.0.0.1:$P_VIEW:6081" \
                -p "127.0.0.1:$P_EXEC:1337" -p "127.0.0.1:$P_PTY:1338" \
                -v "$VOLUME:/home/box" "${mount[@]}" "$ref" >/dev/null
            ;;
        *) "$RT" start "$NAME" >/dev/null ;;
    esac
    local code
    for _ in $(seq 60); do
        if code=$(curl_auth --max-time 2 -o /dev/null -w '%{http_code}' \
                  "http://127.0.0.1:$P_EXEC/health" 2>/dev/null); then
            if [ "$code" = 200 ]; then url; return; fi
        fi
        sleep 0.25
    done
    die "The desktop did not become healthy; inspect box.sh health and the container's service logs."
}

vm_ip() {
    preflight
    if [ "$RT" = container ]; then
        container inspect "$NAME" | python3 -c 'import json,sys
networks=json.load(sys.stdin)[0]["status"].get("networks", [])
addresses=[n["ipv4Address"].split("/")[0] for n in networks if n.get("ipv4Address")]
if not addresses: sys.exit("The desktop has no VM address; run box.sh start first.")
print(addresses[0])'
    else
        "$RT" inspect "$NAME" | python3 -c 'import json,sys
networks=json.load(sys.stdin)[0]["NetworkSettings"]["Networks"]
addresses=[n["IPAddress"] for n in networks.values() if n.get("IPAddress")]
if not addresses: sys.exit("The desktop has no VM address; run box.sh start first.")
print(addresses[0])'
    fi
}

copy_secret() {
    ensure_secrets
    if command -v pbcopy >/dev/null 2>&1; then pbcopy <"$SECRET_DIR/$1"
    elif command -v wl-copy >/dev/null 2>&1; then wl-copy <"$SECRET_DIR/$1"
    elif command -v xclip >/dev/null 2>&1; then xclip -selection clipboard <"$SECRET_DIR/$1"
    else die "No clipboard tool is available; read the private file in the box.sh secret-dir folder."; fi
    printf '%s copied to the clipboard\n' "$2"
}

run_exec() {
    [ "$#" -ge 1 ] || die "Usage: box.sh exec 'command'."
    curl_auth --max-time 610 -X POST -H 'Content-Type: application/json' \
        --data-binary @/dev/fd/3 "http://127.0.0.1:$P_EXEC/exec" \
        3< <(python3 -c 'import json,sys; print(json.dumps({"cmd":sys.argv[1],"timeout":600}))' "$*") \
        | python3 -c 'import json,sys
try:
    reply=json.load(sys.stdin)
except ValueError:
    sys.exit("The desktop returned no valid command response; check box.sh health.")
if not isinstance(reply, dict): sys.exit("The desktop returned an invalid command response.")
if "error" in reply: sys.exit("box: " + str(reply["error"]))
if (not isinstance(reply.get("stdout"), str) or not isinstance(reply.get("stderr"), str)
        or type(reply.get("exit_code")) is not int):
    sys.exit("The desktop returned an incomplete command response.")
sys.stdout.write(reply["stdout"])
sys.stderr.write(reply["stderr"])
sys.exit(reply["exit_code"])'
}

helper() {
    local ref=$1 dir=$2 mode=$3
    shift 3
    local mount=() size=() progress=()
    if [ "$RT" = container ]; then
        local ro=
        [ "$mode" != ro ] || ro=,readonly
        mount=(--mount "type=bind,source=$dir,target=/snap$ro")
        size=(-c 2 -m 512M)
        progress=(--progress none)
    else
        mount=(-v "$dir:/snap:$mode")
        size=(--cpus 2 --memory 512m)
    fi
    # owner-checked: the desktop was removed first; this short-lived helper reuses only its own NAME and home.
    "$RT" run --rm --name "$NAME" "${size[@]}" ${progress[@]+"${progress[@]}"} --entrypoint python3 \
        -v "$VOLUME:/home/box" "${mount[@]}" "$ref" /opt/wingman/snapshot.py "$@"
}

snapshot() {
    preflight
    local out=${1:-"$PWD/wingman-home-$(date +%Y%m%d-%H%M%S).tar"}
    local dir base was ref digest result=0
    dir=$(cd "$(dirname "$out")" && pwd)
    base=$(basename "$out")
    "$RT" volume inspect "$VOLUME" >/dev/null 2>&1 || die "There is no home volume yet; run box.sh start first."
    ref=$(image_ref)
    digest=$(image_digest "$ref")
    was=$(state)
    if [ "$was" != absent ]; then
        ref=$(running_image)
        digest=$(running_digest)
        stop >/dev/null
    fi
    helper "$ref" "$dir" rw save "/snap/$base" "$ref" "$digest" || result=$?
    if [ "$was" = running ]; then start || return 1; fi
    [ "$result" -eq 0 ] || return "$result"
    printf 'snapshot: %s/%s\n' "$dir" "$base"
}

restore() {
    preflight
    local source=${1:-} yes=0 force=()
    [ -n "$source" ] && [ -f "$source" ] || die "Usage: box.sh restore file.tar --yes [--force]."
    shift
    local option
    for option in "$@"; do
        case "$option" in
            --yes) yes=1 ;;
            --force) force=(--force) ;;
            *) die "Usage: box.sh restore file.tar --yes [--force]." ;;
        esac
    done
    [ "$yes" -eq 1 ] || die "Restore replaces the box home; rerun with --yes."
    local dir base was ref digest result=0
    dir=$(cd "$(dirname "$source")" && pwd)
    base=$(basename "$source")
    ref=$(image_ref)
    have_image "$ref" || pull
    digest=$(image_digest "$ref")
    was=$(state)
    [ "$was" = absent ] || stop >/dev/null
    "$RT" volume inspect "$VOLUME" >/dev/null 2>&1 || "$RT" volume create "$VOLUME" >/dev/null
    helper "$ref" "$dir" ro restore "/snap/$base" "$ref" "$digest" ${force[@]+"${force[@]}"} || result=$?
    if [ "$was" = running ]; then start || return 1; fi
    [ "$result" -eq 0 ] || return "$result"
    printf 'restored: %s/%s\n' "$dir" "$base"
}

install_claude() {
    preflight
    run_exec 'set -euo pipefail
        export PATH="$HOME/.local/bin:$PATH"
        if ! command -v claude >/dev/null 2>&1; then
            curl -fsSL https://claude.ai/install.sh | bash
        fi
        claude --version'
}

case "${1:-help}" in
    build)
        preflight
        dns_options
        tag=${2:-wingman-computer:dev}
        if [ "$RT" = container ]; then
            "$RT" build ${DNS_ARGS[@]+"${DNS_ARGS[@]}"} -t "$tag" -f "$HERE/Containerfile" "$HERE"
        else
            [ -z "${WINGMAN_DNS:-}" ] || die "For Docker/Podman builds, configure the builder's DNS before building."
            "$RT" build -t "$tag" -f "$HERE/Containerfile" "$HERE"
        fi
        printf 'built %s\n' "$tag"
        ;;
    pull) pull ;;
    start) start ;;
    stop) stop ;;
    status)
        preflight
        current=$(state)
        printf '%s\n' "$current"
        if [ "$current" != absent ]; then
            printf 'image: %s\nimage_digest: %s\n' "$(running_image)" "$(running_digest)"
        fi
        [ "$current" != running ] || url
        ;;
    health) curl_auth --max-time 10 "http://127.0.0.1:$P_EXEC/health"; printf '\n' ;;
    exec) shift; run_exec "$@" ;;
    screenshot)
        out=${2:-"$PWD/wingman-screen-$(date +%H%M%S).png"}
        part="$out.part"
        if curl_auth --max-time 30 -o "$part" "http://127.0.0.1:$P_EXEC/screenshot"; then
            mv "$part" "$out"
            printf '%s\n' "$out"
        else
            result=$?
            rm -f "$part"
            exit "$result"
        fi
        ;;
    snapshot) shift; snapshot "$@" ;;
    restore) shift; restore "$@" ;;
    install-claude) install_claude ;;
    vm-ip) vm_ip ;;
    secret-dir) printf '%s\n' "$SECRET_DIR" ;;
    password) copy_secret vnc 'VNC password' ;;
    token) copy_secret token 'Box token' ;;
    image|ref) image_ref ;;
    url) url ;;
    shell)
        preflight
        exec "$RT" exec -it -u box -w /home/box -e HOME=/home/box \
            -e DISPLAY=:1 -e XDG_RUNTIME_DIR=/run/user/1000 \
            -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
            -e PATH=/home/box/.local/bin:/usr/local/bin:/usr/bin:/bin "$NAME" \
            python3 -I /opt/wingman/cpu-session.py bash -l
        ;;
    help|-h|--help)
        cat <<'EOF'
box.sh build [tag]                  build locally (default: wingman-computer:dev)
box.sh start | stop | status        manage this desktop, preserving its home
box.sh pull                        fetch the image pinned in image.lock
box.sh health                      authenticated JSON desktop report
box.sh exec 'command'               run as box inside the desktop session
box.sh shell                       an interactive desktop-session shell
box.sh screenshot [file.png]        save the actual X11 screen
box.sh snapshot [file.tar]          archive home and image identity, excluding known credentials
box.sh restore file.tar --yes       replace home (add --force for a different image digest)
box.sh install-claude               install Claude Code on first use; sign in inside the box
box.sh vm-ip | secret-dir           VM address or private host credential directory
box.sh password | token            copy a credential to the clipboard, never print it
box.sh image | ref | url            image reference or noVNC address
EOF
        ;;
    *) printf 'Unknown command: %s (try box.sh help).\n' "$1" >&2; exit 2 ;;
esac
