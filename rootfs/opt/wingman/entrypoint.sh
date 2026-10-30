#!/bin/bash
set -euo pipefail
LOG=/var/log/wingman
CONF=/etc/wingman
BIND=${WINGMAN_BIND:-127.0.0.1}
GEOMETRY=${BOX_GEOMETRY:-1920x1200x24}
export XDG_RUNTIME_DIR=/run/user/1000
DBUS="unix:path=$XDG_RUNTIME_DIR/bus"
mkdir -p "$LOG" "$CONF" /run/wingman
chown root:box "$CONF"
chmod 0750 "$CONF"
chown box:box /run/wingman
chmod 0750 /run/wingman

if [ ! -f /home/box/.bashrc ]; then
    cp -a /opt/box-skel/. /home/box/
fi
for d in agent-data wingman-data chrome-profile out reference; do
    [ -d "/home/box/$d" ] || install -d -o box -g box "/home/box/$d"
done
chown box:box /home/box

# The mounted host copies are never passed to a process in its arguments or environment.
python3 - <<'PY'
import os
import secrets
import string
from pathlib import Path

conf = Path("/etc/wingman")
for name, length in (("vnc", 8), ("token", 43)):
    source = Path("/run/wingman-secret") / name
    target = conf / name
    if source.is_file():
        value = source.read_bytes().strip()
    elif target.is_file():
        value = target.read_bytes().strip()
    else:
        value = "".join(secrets.choice(string.ascii_letters + string.digits)
                        for _ in range(length)).encode()
    if len(value) != length or not all(chr(c) in string.ascii_letters + string.digits + "-_" for c in value):
        raise SystemExit(f"wingman: the {name} file is invalid")
    temporary = conf / (name + ".new")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        os.fchmod(stream.fileno(), 0o640)
        os.fchown(stream.fileno(), 0, 1000)
    os.replace(temporary, target)
PY

# These are stale sockets from this container's own X display, never host files.
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix
install -d -m 0700 -o box -g box "$XDG_RUNTIME_DIR"

as_box() {
    runuser -u box -- env -i HOME=/home/box USER=box LOGNAME=box SHELL=/bin/bash \
        PATH=/home/box/.local/bin:/usr/local/bin:/usr/bin:/bin \
        LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}" TZ="${TZ:-America/Los_Angeles}" \
        DISPLAY=:1 XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" DBUS_SESSION_BUS_ADDRESS="$DBUS" \
        XDG_CURRENT_DESKTOP=XFCE XDG_SESSION_TYPE=x11 "$@"
}

WORKERS=()
keep() {
    local name=$1
    shift
    (
        child=
        finish() {
            if [ -n "$child" ]; then
                # owner-checked: child is the command launched by this supervisor iteration.
                kill -TERM "$child" 2>/dev/null || true
                wait "$child" 2>/dev/null || true
            fi
            exit 0
        }
        trap finish TERM INT
        while true; do
            "$@" >>"$LOG/$name.log" 2>&1 &
            child=$!
            rc=0
            wait "$child" || rc=$?
            child=
            echo "$(date -Is) $name exited ($rc), restarting" >>"$LOG/supervisor.log"
            sleep 1
        done
    ) &
    WORKERS+=("$!")
}

finish() {
    trap - EXIT TERM INT
    local pid
    for pid in "${WORKERS[@]}"; do
        # owner-checked: every pid in WORKERS was launched by keep() in this container.
        kill -TERM "$pid" 2>/dev/null || true
    done
    wait || true
}
trap 'finish; exit 0' TERM INT
trap finish EXIT

wait_until() {
    local name=$1
    shift
    for _ in $(seq 100); do
        if "$@" >/dev/null 2>&1; then return; fi
        sleep 0.05
    done
    echo "wingman: $name did not become ready; see /var/log/wingman" >&2
    exit 1
}

keep xvfb Xvfb :1 -screen 0 "$GEOMETRY" -nolisten tcp -dpi 96 +extension RENDER +extension Composite
wait_until display as_box xdpyinfo
keep dbus as_box dbus-daemon --session --nofork --nopidfile --address="$DBUS"
wait_until dbus test -S "$XDG_RUNTIME_DIR/bus"
keep wallpaper as_box /opt/wingman/wallpaper.sh
keep xfwm4 as_box xfwm4 --compositor=off --replace
wait_until window-manager as_box wmctrl -m
keep picom as_box picom --config /etc/xdg/picom.conf
wait_until compositor as_box python3 /opt/wingman/compositor-ready.py

# Apply the locked panel contract to existing home volumes as well as fresh homes.
as_box xfconf-query -c xfce4-panel -p /panels/panel-1/position --create -t string -s 'p=10;x=0;y=0'
as_box xfconf-query -c xfce4-panel -p /panels/panel-1/size --create -t uint -s 40
as_box xfconf-query -c xfce4-panel -p /panels/panel-1/icon-size --create -t uint -s 24
keep panel as_box xfce4-panel --disable-wm-check
keep plank as_box plank
keep pulse as_box pulseaudio --daemonize=no --exit-idle-time=-1 --disallow-exit

# Raw RFB never listens on the VM's network address, even when HTTP is published.
keep x11vnc as_box x11vnc -display :1 -listen 127.0.0.1 -localhost -rfbport 5900 \
    -passwdfile "$CONF/vnc" -forever -shared -noxdamage -noshm -quiet
keep x11vnc-view as_box x11vnc -display :1 -listen 127.0.0.1 -localhost -rfbport 5901 \
    -passwdfile "$CONF/vnc" -viewonly -forever -shared -noxdamage -noshm -quiet
keep novnc websockify --web /usr/share/novnc "$BIND:6080" 127.0.0.1:5900
keep novnc-view websockify --web /usr/share/novnc "$BIND:6081" 127.0.0.1:5901
keep wingmand as_box env WINGMAN_BIND="$BIND" WINGMAN_IMAGE_DIGEST="${WINGMAN_IMAGE_DIGEST:-}" \
    python3 -I /opt/wingman/wingmand.py
echo "$(date -Is) desktop services started" >>"$LOG/supervisor.log"
wait
