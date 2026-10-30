#!/bin/bash
# Wingman Computer: pull, start and reach the Debian desktop box on this machine.
# Runs under Apple's `container` CLI on a Mac, or Docker/Podman elsewhere.
# Usage: box.sh <command>   (box.sh help lists them)
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
NAME=${WINGMAN_COMPUTER_NAME:-wingman-computer}
VOLUME=${WINGMAN_COMPUTER_VOLUME:-$NAME-home}
LOCK=${WINGMAN_COMPUTER_LOCK:-$HERE/image.lock}
CPUS=${WINGMAN_CPUS:-4}
MEMORY=${WINGMAN_MEMORY:-6G}
P_NOVNC=${WINGMAN_NOVNC_PORT:-6080}
P_VIEW=${WINGMAN_VIEW_PORT:-6081}
P_EXEC=${WINGMAN_EXEC_PORT:-1337}
P_PTY=${WINGMAN_PTY_PORT:-1338}
if [ "$(uname -s)" = Darwin ]; then
    SUPPORT="$HOME/Library/Application Support/Wingman-Computer"
else
    SUPPORT="${XDG_CONFIG_HOME:-$HOME/.config}/wingman-computer"
fi
SECRET_DIR="$SUPPORT/secret"

# ---- runtime: Apple container, else docker, else podman (WINGMAN_RUNTIME overrides)
RT=${WINGMAN_RUNTIME:-}
if [ -z "$RT" ]; then
    for c in container docker podman; do command -v "$c" >/dev/null 2>&1 && { RT=$c; break; }; done
fi
[ -n "$RT" ] || { echo "needs Apple's container CLI (brew install container), Docker or Podman" >&2; exit 1; }

need_service() { [ "$RT" != container ] || container system status >/dev/null 2>&1 || container system start >/dev/null; }

# ---- the image: a digest from image.lock, never a moving tag (WINGMAN_COMPUTER_IMAGE overrides)
image_ref() {
    if [ -n "${WINGMAN_COMPUTER_IMAGE:-}" ]; then echo "$WINGMAN_COMPUTER_IMAGE"; return; fi
    [ -f "$LOCK" ] || { echo "no image.lock at $LOCK" >&2; exit 1; }
    local img dig
    img=$(sed -n 's/^image=//p' "$LOCK"); dig=$(sed -n 's/^digest=//p' "$LOCK")
    case "$dig" in sha256:????????????????????????????????????????????????????????????????) ;;
        *) echo "image.lock has no pinned sha256 digest" >&2; exit 1 ;; esac
    echo "$img@$dig"
}

have_image() {
    if [ "$RT" = container ]; then container image inspect "$1" >/dev/null 2>&1
    else "$RT" image inspect "$1" >/dev/null 2>&1; fi
}

pull() {
    need_service
    local ref; ref=$(image_ref)
    echo "pulling $ref"
    "$RT" image pull "$ref"
    have_image "$ref" && echo "have $ref"
}

ensure_secrets() {  # made once, kept 0600 on this machine, never printed
    mkdir -p "$SECRET_DIR"; chmod 700 "$SUPPORT" "$SECRET_DIR"
    local f
    for f in vnc:8 token:43; do
        local name=${f%%:*} len=${f##*:}
        if [ ! -s "$SECRET_DIR/$name" ]; then
            ( umask 077; set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$len" >"$SECRET_DIR/$name" )
        fi
        chmod 600 "$SECRET_DIR/$name"
    done
}

state() {
    if [ "$RT" = container ]; then
        container inspect "$NAME" 2>/dev/null | python3 -c 'import json,sys
d=json.load(sys.stdin); s=d[0]["status"] if d else "absent"
print(s.get("state", "unknown") if isinstance(s, dict) else s)' 2>/dev/null || echo absent
    else
        "$RT" inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || echo absent
    fi
}

running_image() {
    if [ "$RT" = container ]; then
        container inspect "$NAME" 2>/dev/null | python3 -c 'import json,sys
d=json.load(sys.stdin); print(d[0]["configuration"]["image"]["reference"] if d else "")' 2>/dev/null || true
    else
        "$RT" inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null || true
    fi
}

curl_auth() {  # curl with the box token, passed on a private pipe so it never shows in argv
    curl -sS -K <(printf 'header = "Authorization: Bearer %s"\n' "$(cat "$SECRET_DIR/token")") "$@"
}

start() {
    need_service; ensure_secrets
    local ref; ref=$(image_ref)
    have_image "$ref" || pull
    "$RT" volume inspect "$VOLUME" >/dev/null 2>&1 || "$RT" volume create "$VOLUME" >/dev/null
    local st; st=$(state)
    if [ "$st" != absent ] && [ "$(running_image)" != "$ref" ]; then
        echo "the box was made from another image; recreating it (your home volume stays)"
        "$RT" stop "$NAME" >/dev/null 2>&1 || true; "$RT" rm "$NAME" >/dev/null 2>&1 || true; st=absent
    fi
    case "$st" in
        running) echo "already running" ;;
        absent)
            local mount
            if [ "$RT" = container ]; then mount=(--mount "type=bind,source=$SECRET_DIR,target=/run/wingman-secret,readonly")
            else mount=(-v "$SECRET_DIR:/run/wingman-secret:ro"); fi
            local size=(-c "$CPUS" -m "$MEMORY")
            [ "$RT" = container ] || size=(--cpus "$CPUS" --memory "$MEMORY" --shm-size 1g)
            "$RT" run -d --name "$NAME" "${size[@]}" \
                -e WINGMAN_BIND=0.0.0.0 \
                -p "127.0.0.1:$P_NOVNC:6080" -p "127.0.0.1:$P_VIEW:6081" \
                -p "127.0.0.1:$P_EXEC:1337" -p "127.0.0.1:$P_PTY:1338" \
                -v "$VOLUME:/home/box" "${mount[@]}" "$ref" >/dev/null ;;
        *) "$RT" start "$NAME" >/dev/null ;;
    esac
    local t0=$SECONDS
    for _ in $(seq 120); do
        if [ "$(curl_auth -o /dev/null -w '%{http_code}' "http://127.0.0.1:$P_EXEC/health" 2>/dev/null)" = 200 ]; then
            echo "ready in $((SECONDS - t0)) s: $(url)"; return 0
        fi
        sleep 1
    done
    echo "started, but the desktop did not report healthy in 120 s (try: box.sh health)" >&2; return 1
}

stop() { need_service; "$RT" stop "$NAME" >/dev/null 2>&1 || true; "$RT" rm "$NAME" >/dev/null 2>&1 || true; echo stopped; }
url()  { echo "http://127.0.0.1:$P_NOVNC/vnc.html?autoconnect=1&resize=scale"; }

copy_secret() {
    ensure_secrets
    if command -v pbcopy >/dev/null; then pbcopy <"$SECRET_DIR/$1"
    elif command -v wl-copy >/dev/null; then wl-copy <"$SECRET_DIR/$1"
    elif command -v xclip >/dev/null; then xclip -selection clipboard <"$SECRET_DIR/$1"
    else echo "no clipboard tool; the $1 is in $SECRET_DIR/$1" >&2; return 1; fi
    echo "$2 copied to the clipboard"
}

run_exec() {  # box.sh exec 'command' : runs as box through the exec endpoint, prints its output
    [ $# -ge 1 ] || { echo "usage: box.sh exec 'command'" >&2; exit 2; }
    python3 -c 'import json,sys; print(json.dumps({"cmd": sys.argv[1], "timeout": 600}))' "$*" \
        | curl_auth -X POST -H 'Content-Type: application/json' --data-binary @- "http://127.0.0.1:$P_EXEC/exec" \
        | python3 -c 'import json,sys
r=json.load(sys.stdin)
if "error" in r: sys.exit("box: " + r["error"])
sys.stdout.write(r["stdout"]); sys.stderr.write(r["stderr"]); sys.exit(r["exit_code"] if r["exit_code"] is not None else 1)'
}

helper() {  # helper <host dir> <ro|rw> <script> [args...]: a throwaway box with the home volume and a host folder at /snap
    # The script is fixed text; file names travel as positional arguments ($1...), never inside the script.
    local ref; ref=$(image_ref); have_image "$ref" || pull
    local dir=$1 mode=$2 script=$3; shift 3
    if [ "$RT" = container ]; then
        local ro=""; [ "$mode" = ro ] && ro=",readonly"
        container run --rm --entrypoint /bin/bash -v "$VOLUME:/home/box" \
            --mount "type=bind,source=$dir,target=/snap$ro" "$ref" -c "$script" helper "$@"
    else
        "$RT" run --rm --entrypoint /bin/bash -v "$VOLUME:/home/box" -v "$dir:/snap:$mode" "$ref" -c "$script" helper "$@"
    fi
}

snapshot() {  # snapshot [file.tar]: saves /home/box to a tar on this machine
    need_service
    local out=${1:-"$PWD/wingman-home-$(date +%Y%m%d-%H%M%S).tar"}
    case "$out" in /*) ;; *) out="$PWD/$out" ;; esac
    "$RT" volume inspect "$VOLUME" >/dev/null 2>&1 || { echo "no home volume $VOLUME yet" >&2; exit 1; }
    local was; was=$(state)
    [ "$was" = running ] && { echo "stopping the box so the snapshot is consistent"; stop >/dev/null; }
    local dir; dir=$(cd "$(dirname "$out")" && pwd); local base; base=$(basename "$out")
    helper "$dir" rw 'set -e; tar --numeric-owner -C /home/box -cpf "/snap/$1.part" . && mv "/snap/$1.part" "/snap/$1"' "$base"
    echo "snapshot: $out ($(du -h "$out" | cut -f1))"
    [ "$was" = running ] && start || true
}

restore() {  # restore <file.tar> [--yes]: replaces /home/box with the snapshot
    need_service
    local in=${1:-}; [ -f "$in" ] || { echo "usage: box.sh restore <snapshot.tar> [--yes]" >&2; exit 2; }
    if [ "${2:-}" != --yes ] && [ "${WINGMAN_YES:-}" != 1 ]; then
        printf 'This replaces everything in the box home folder with %s. Type yes to go on: ' "$in"
        read -r ans; [ "$ans" = yes ] || { echo "nothing changed"; exit 1; }
    fi
    local was; was=$(state)
    [ "$was" = running ] && stop >/dev/null
    "$RT" volume inspect "$VOLUME" >/dev/null 2>&1 || "$RT" volume create "$VOLUME" >/dev/null
    local dir; dir=$(cd "$(dirname "$in")" && pwd); local base; base=$(basename "$in")
    # The snapshot is unpacked into a staging folder first. The old home is cleared only after that
    # succeeds, so a bad or truncated tar leaves the home as it was.
    # owner-checked: the rm below only touches the box's own home volume ($VOLUME), after the user said yes
    helper "$dir" ro 'set -e
        f="/snap/$1"; stage=/home/box/.wingman-restore-stage
        rm -rf "$stage"; tar -tf "$f" >/dev/null; mkdir "$stage"
        tar --numeric-owner -C "$stage" -xpf "$f" || { rm -rf "$stage"; echo "the snapshot did not unpack; home unchanged" >&2; exit 1; }
        find /home/box -mindepth 1 -maxdepth 1 ! -path "$stage" -exec rm -rf {} +
        find "$stage" -mindepth 1 -maxdepth 1 -exec mv -t /home/box {} +
        rmdir "$stage"' "$base"
    echo "restored $in into $VOLUME"
    [ "$was" = running ] && start || true
}

install_claude() {  # runs Anthropic's official installer inside the box, as box. Sign-in stays with the user.
    need_service
    "$RT" exec -u box -w /home/box -e HOME=/home/box "$NAME" bash -lc \
        'curl -fsSL https://claude.ai/install.sh | bash && ~/.local/bin/claude --version'
    echo "Claude Code is installed in the box. Run 'claude' in the box terminal to sign in."
}

case "${1:-help}" in
    pull) pull ;;
    start) start ;;
    stop) stop ;;
    status) need_service; s=$(state); echo "$s"; [ "$s" = running ] && { url; echo "image: $(running_image)"; } || true ;;
    url) url ;;
    shell) need_service; exec "$RT" exec -it -u box -w /home/box -e HOME=/home/box "$NAME" bash -l ;;
    password) copy_secret vnc "VNC password" ;;
    token) copy_secret token "box token" ;;
    health) ensure_secrets; curl_auth "http://127.0.0.1:$P_EXEC/health"; echo ;;
    screenshot) ensure_secrets; out=${2:-"$PWD/wingman-screen-$(date +%H%M%S).png"}
        curl_auth -f -o "$out" "http://127.0.0.1:$P_EXEC/screenshot" && echo "$out" ;;
    exec) shift; run_exec "$@" ;;
    install-claude) install_claude ;;
    snapshot) shift; snapshot "$@" ;;
    restore) shift; restore "$@" ;;
    image) image_ref ;;
    build) need_service; tag=${2:-wingman-computer:dev}
        if [ "$RT" = container ]; then container build -t "$tag" -f "$HERE/Containerfile" "$HERE"
        else "$RT" build -t "$tag" -f "$HERE/Containerfile" "$HERE"; fi
        echo "built $tag (run it with WINGMAN_COMPUTER_IMAGE=$tag box.sh start)" ;;
    help|-h|--help) cat <<'EOF'
box.sh pull            fetch the image pinned in image.lock (by digest)
box.sh start           start the box (pulls first if needed); prints the noVNC address
box.sh stop            stop and remove the box; the home volume stays
box.sh status          running, stopped or absent, and the image in use
box.sh url             the noVNC address
box.sh password        copy the VNC password to the clipboard
box.sh token           copy the box token (exec, pty, health) to the clipboard
box.sh health          the desktop health report
box.sh screenshot [f]  save the screen as a PNG
box.sh exec 'cmd'      run a command as box and print its output
box.sh shell           a bash shell inside, as box
box.sh install-claude  install Claude Code inside the box with Anthropic's installer
box.sh snapshot [f]    save the box home folder to a tar
box.sh restore f       replace the box home folder with a snapshot tar
box.sh image           the image reference in use
box.sh build [tag]     build the image locally from the Containerfile
EOF
    ;;
    *) echo "unknown command: $1 (try box.sh help)" >&2; exit 2 ;;
esac
