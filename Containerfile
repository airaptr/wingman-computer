# Wingman Computer: a Debian 13 desktop you open in a browser.
# One image for linux/arm64 and linux/amd64. Our own build from public Debian packages.
# The base is pinned by digest (the multi-arch index of debian:trixie).
FROM debian:trixie@sha256:2dc419c6d7fe3dad0a2f9fd469c34cc90b0f9ba642d8fde837fe3d7d34a0e7f1

ARG TARGETARCH
ARG NODE_VERSION=24.22.2
ARG NODE_SHA256_ARM64=eb187837a2230d4047161ac0be008584e16b46f726e9c9490ee35dc6b17d9ba8
ARG NODE_SHA256_AMD64=48cc0e21e048790e54c0cf5749c2725005313d652651c40b7a21feb9697aaf75
ARG UV_VERSION=0.13.6
ARG UV_SHA256_ARM64=229e194255c7958d86d56e8790d969a6f74736592133f0dfd0076f754c642c1d
ARG UV_SHA256_AMD64=f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645

LABEL org.opencontainers.image.source="https://github.com/airaptr/wingman-computer" \
      org.opencontainers.image.description="Wingman Computer: a Debian 13 desktop (Xfce panel, Plank dock, Chromium, LibreOffice) reached through noVNC" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Wildhaven Homes LLC"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=America/Los_Angeles \
    DISPLAY=:1

# Desktop, apps, fonts and tools. Chromium on both architectures (Google Chrome has no arm64 Linux build).
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify xauth x11-utils x11-xserver-utils x11-apps \
        xfwm4 xfce4-panel xfce4-terminal thunar plank picom hsetroot \
        dbus dbus-x11 dbus-user-session at-spi2-core dconf-cli desktop-file-utils xdg-utils \
        adwaita-icon-theme gnome-themes-extra gnome-keyring pinentry-curses \
        fonts-dejavu-core fonts-liberation fonts-liberation-sans-narrow fonts-noto-cjk \
        fonts-noto-color-emoji fonts-droid-fallback fonts-opensymbol fontconfig \
        chromium libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-gtk3 \
        pulseaudio \
        python3 python3-pip python3-venv git jq ripgrep tmux xdotool wmctrl poppler-utils \
        postgresql-client rustc cargo libssl-dev patch unzip xz-utils openssh-client \
        curl wget ca-certificates gnupg sudo procps psmisc iproute2 acl bubblewrap tzdata less \
    && { apt-get purge -y light-locker xfce4-screensaver xfce4-power-manager 2>/dev/null || true; } \
    && ln -sf /usr/share/zoneinfo/America/Los_Angeles /etc/localtime && echo America/Los_Angeles > /etc/timezone \
    && find /var/lib/apt/lists -mindepth 1 -delete

# GitHub CLI from GitHub's signed apt repository. Sign-in happens later, by the user, inside the box.
RUN install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 20 \
         https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod 0644 /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends gh \
    && find /var/lib/apt/lists -mindepth 1 -delete

# Node.js 24 and uv, from their official release files, checked against pinned SHA-256 sums.
RUN set -eu; \
    case "$TARGETARCH" in \
      arm64) n=arm64; ns=$NODE_SHA256_ARM64; u=aarch64; us=$UV_SHA256_ARM64 ;; \
      amd64) n=x64;   ns=$NODE_SHA256_AMD64; u=x86_64;  us=$UV_SHA256_AMD64 ;; \
      *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSLo node.tar.xz --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 20 \
        "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$n.tar.xz"; \
    echo "$ns  node.tar.xz" | sha256sum -c -; \
    tar -xJf node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \
    curl -fsSLo uv.tar.gz --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 20 \
        "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$u-unknown-linux-gnu.tar.gz"; \
    echo "$us  uv.tar.gz" | sha256sum -c -; \
    tar -xzf uv.tar.gz --strip-components=1 -C /usr/local/bin --no-same-owner; \
    rm -f node.tar.xz uv.tar.gz; \
    rm -f /usr/local/CHANGELOG.md /usr/local/README.md; \
    node --version; npm --version; uv --version

# Keep source and desktop edits after the slow package/download layers.
COPY tools/image-hygiene.py /tmp/wingman-image-hygiene.py
RUN python3 /tmp/wingman-image-hygiene.py \
    && rm -f /tmp/wingman-image-hygiene.py \
    && if dpkg-query -W -f='${db:Status-Status}' ssl-cert 2>/dev/null | grep -qx installed; then \
         apt-get purge -y ssl-cert; \
       fi \
    && test ! -e /etc/ssl/private/ssl-cert-snakeoil.key \
    && npm config get registry >/dev/null

# The one user. Home is a volume, so the skeleton is kept aside and copied in when the volume is empty.
COPY LICENSE NOTICE /usr/share/doc/wingman-computer/
COPY rootfs/ /
RUN useradd -m -u 1000 -s /bin/bash box \
    && echo 'box ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/box && chmod 0440 /etc/sudoers.d/box \
    && cp -a /etc/skel-wingman/. /home/box/ \
    && mkdir -p /home/box/agent-data /home/box/wingman-data /home/box/chrome-profile \
                /home/box/out /home/box/reference \
    && chown -R box:box /home/box \
    && cp -a /home/box /opt/box-skel \
    && chmod 0755 /opt/wingman/*.sh /opt/wingman/*.py /usr/local/bin/wingman-browser \
    && dconf update && update-desktop-database \
    && python3 -c 'from pathlib import Path; [compile(p.read_text(), str(p), "exec") for p in Path("/opt/wingman").glob("*.py")]'

# Xvfb provides both the depth-24 root and depth-32 ARGB visuals.
ENV BOX_GEOMETRY=1920x1200x24

# 6080 noVNC (control), 6081 noVNC (view only), 1337 exec, 1338 pty.
# The services listen on 127.0.0.1 unless WINGMAN_BIND says otherwise; every one needs the box token.
EXPOSE 6080 6081 1337 1338
ENTRYPOINT ["python3", "-I", "/opt/wingman/cpu-session.py", "/opt/wingman/entrypoint.sh"]
