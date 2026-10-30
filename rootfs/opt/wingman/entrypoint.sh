#!/bin/bash
# Wingman Computer supervisor. Starts the desktop and the box services, and restarts any part that stops.
set -u
LOG=/var/log/wingman
SECRET_IN=/run/wingman-secret      # optional read-only mount from the host (box.sh mounts one)
CONF=/etc/wingman                  # root:box 0750; files root:box 0640
BIND=${WINGMAN_BIND:-127.0.0.1}    # where the published listeners bind inside the box
GEOMETRY=${BOX_GEOMETRY:-1920x1200x24+32}   # +32: 4-byte pixels, so ARGB windows composite cleanly
export XDG_RUNTIME_DIR=/run/user/1000
mkdir -p "$LOG" "$CONF" /run/wingman
chown root:box "$CONF"; chmod 0750 "$CONF"
chown box:box /run/wingman; chmod 0750 /run/wingman

# Home lives on a volume. Copy the skeleton in the first time, and add any folder a newer image brings.
if [ ! -f /home/box/.bashrc ]; then
    cp -a /opt/box-skel/. /home/box/
fi
for d in agent-data wingman-data chrome-profile out reference cli-config .claude; do
    [ -d "/home/box/$d" ] || install -d -o box -g box "/home/box/$d"
done
chown box:box /home/box

# Secrets: take the host's copy when mounted, otherwise make one at first boot. Never printed or logged.
# secret <name> <length>: writes $CONF/<name>, root:box 0640.
secret() {
    local name=$1 len=$2 out="$CONF/$1"
    if [ -s "$SECRET_IN/$name" ]; then
        ( umask 027; head -n1 "$SECRET_IN/$name" | tr -d '\r\n' > "$out" )
    elif [ ! -s "$out" ]; then
        ( umask 027; set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$len" > "$out" )
        echo "$(date -Is) made a new $name at first boot (read it with: cat $out, as root)" >>"$LOG/supervisor.log"
    fi
    chown root:box "$out"; chmod 0640 "$out"
}
secret vnc 8        # VNC passwords are 8 characters at most
secret token 43     # bearer token for the exec, pty and health endpoints

rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
install -d -m 0700 -o box -g box "$XDG_RUNTIME_DIR"
DBUS="unix:path=$XDG_RUNTIME_DIR/bus"

as_box() {
    runuser -u box -- env -i HOME=/home/box USER=box LOGNAME=box SHELL=/bin/bash \
        PATH=/usr/local/bin:/usr/bin:/bin LANG="$LANG" LC_ALL="$LANG" TZ="$TZ" DISPLAY=:1 \
        XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" DBUS_SESSION_BUS_ADDRESS="$DBUS" \
        XDG_CURRENT_DESKTOP=XFCE XDG_SESSION_TYPE=x11 "$@"
}

keep() {  # keep <name> <command...>: run the command and restart it whenever it exits
    local name=$1; shift
    ( while true; do
        "$@" >>"$LOG/$name.log" 2>&1
        echo "$(date -Is) $name exited ($?), restarting" >>"$LOG/supervisor.log"
        sleep 1
      done ) &
}

wait_for() { for _ in $(seq 100); do [ -e "$1" ] && return 0; sleep 0.1; done; return 1; }

keep xvfb  Xvfb :1 -screen 0 "$GEOMETRY" -nolisten tcp -dpi 96
wait_for /tmp/.X11-unix/X1
keep dbus  as_box dbus-daemon --session --nofork --nopidfile --address="$DBUS"
wait_for "$XDG_RUNTIME_DIR/bus"

keep wallpaper as_box /opt/wingman/wallpaper.sh
keep xfwm4     as_box xfwm4 --compositor=off --replace
keep panel     as_box xfce4-panel --disable-wm-check
keep picom     as_box picom --backend xrender --no-fading-openclose
keep plank     as_box plank
keep pulse     as_box pulseaudio --daemonize=no --exit-idle-time=-1 --disallow-exit

# -noshm: Xvfb runs as root and x11vnc as box, so shared memory is refused.
keep x11vnc  as_box x11vnc -display :1 -localhost -rfbport 5900 -passwdfile "$CONF/vnc" \
                    -forever -shared -noxdamage -noshm -quiet
keep x11vnc-view as_box x11vnc -display :1 -localhost -rfbport 5901 -passwdfile "$CONF/vnc" \
                    -viewonly -forever -shared -noxdamage -noshm -quiet
keep novnc      websockify --web /usr/share/novnc "$BIND:6080" 127.0.0.1:5900
keep novnc-view websockify --web /usr/share/novnc "$BIND:6081" 127.0.0.1:5901
keep wingmand as_box env WINGMAN_BIND="$BIND" python3 -I /opt/wingman/wingmand.py

echo "$(date -Is) wingman computer up (listeners on $BIND)" >>"$LOG/supervisor.log"
trap 'kill 0; exit 0' TERM INT
wait
