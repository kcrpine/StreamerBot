# StreamerBot runtime image.
#
# Debian 13 (trixie). Python comes from the distro (3.13), so everything below
# installs into /opt/venv -- Debian 13 marks the system interpreter as
# externally managed (PEP 668) and a bare "pip install" is refused.
FROM debian:trixie-slim

# Set by BuildKit. Falls back to amd64 for plain "docker build" without buildx.
ARG TARGETARCH=amd64

# Supplied by streamerbot.sh / update.sh from project.env.
ARG TTSDK_URL_X86_64
ARG TTSDK_URL_ARM64
ARG GO_LIBRESPOT_VERSION=0.9.0

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ---------------------------------------------------------------------------
# System packages.
#   libmpv2       - the vendored mpv.py ctypes binding dlopen()s libmpv.so.2
#   pulseaudio    - the null sink every playback engine feeds
#   libasound2-plugins + /etc/asound.conf - routes go-librespot's ALSA output
#                   into PulseAudio
#   xvfb          - Chrome must run headful to produce audio; it needs a display
#   p7zip-full    - unpacks the TeamTalk SDK .7z
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        gnupg \
        libasound2-plugins \
        libmpv2 \
        p7zip-full \
        procps \
        pulseaudio \
        pulseaudio-utils \
        python3 \
        python3-venv \
        unzip \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

# go-librespot uses the ALSA backend; send it to the default PulseAudio sink.
RUN printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > /etc/asound.conf

RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel

# ENV PATH is lost in login shells, which reset it from /etc/profile. Anything
# run as "bash -l" (a debug shell, a docker exec) would otherwise get the system
# python and none of the bot's dependencies.
RUN printf 'PATH=/opt/venv/bin:$PATH\nexport PATH\n' > /etc/profile.d/streamerbot-venv.sh

# ---------------------------------------------------------------------------
# TeamTalk SDK. Fetched from bearware.dk at build time; the exact archive name
# changes with every point release, so the URLs come from project.env rather
# than being generated from a version number.
# ---------------------------------------------------------------------------
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) TTSDK_URL="$TTSDK_URL_X86_64" ;; \
      arm64) TTSDK_URL="$TTSDK_URL_ARM64" ;; \
      *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    [ -n "$TTSDK_URL" ] || { echo "TeamTalk SDK URL not set for $TARGETARCH" >&2; exit 1; }; \
    curl -fsSL "$TTSDK_URL" -o /tmp/ttsdk.7z; \
    7z x -y /tmp/ttsdk.7z -o/tmp/ttsdk > /dev/null; \
    mkdir -p /opt/teamtalk; \
    cp -a /tmp/ttsdk/*/Library/TeamTalk_DLL/. /opt/teamtalk/; \
    rm -rf /tmp/ttsdk /tmp/ttsdk.7z; \
    test -f /opt/teamtalk/libTeamTalk5.so

ENV LD_LIBRARY_PATH=/opt/teamtalk

# ---------------------------------------------------------------------------
# Node.js 22 for the YouTube bridge and the bgutil proof-of-origin provider.
# ---------------------------------------------------------------------------
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-provider \
    && cd /opt/bgutil-provider/server \
    && npm ci \
    && npx tsc

# ---------------------------------------------------------------------------
# Google Chrome, amd64 only.
#
# Only Google's own build ships the Widevine CDM, and Google publishes no
# linux/arm64 Chrome. Chromium is not a substitute: without Widevine, Netflix,
# Disney Plus, Apple Music and Amazon Music cannot decrypt anything. On arm64
# the marker file below tells the browser engine to disable those four services
# with a readable message instead of failing at playback time.
# ---------------------------------------------------------------------------
RUN set -eux; \
    if [ "$TARGETARCH" = "amd64" ]; then \
      curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg; \
      echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list; \
      apt-get update; \
      apt-get install -y --no-install-recommends google-chrome-stable fonts-liberation libnss3 libgbm1 libxss1 libxtst6 x11-utils; \
      rm -rf /var/lib/apt/lists/*; \
      echo 1 > /etc/streamerbot-browser-available; \
    else \
      echo 0 > /etc/streamerbot-browser-available; \
    fi

# ---------------------------------------------------------------------------
# go-librespot: the Spotify Connect daemon behind the librespot engine.
# arm64 builds exist, so Spotify works even where the browser services do not.
# ---------------------------------------------------------------------------
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) LS_ARCH=linux_x86_64 ;; \
      arm64) LS_ARCH=linux_arm64 ;; \
      *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/go-librespot.tar.gz \
      "https://github.com/devgianlu/go-librespot/releases/download/v${GO_LIBRESPOT_VERSION}/go-librespot_${LS_ARCH}.tar.gz"; \
    tar -xzf /tmp/go-librespot.tar.gz -C /usr/local/bin go-librespot; \
    chmod 0755 /usr/local/bin/go-librespot; \
    rm -f /tmp/go-librespot.tar.gz; \
    test -x /usr/local/bin/go-librespot

RUN useradd -ms /bin/bash streamer
WORKDIR /home/streamer/StreamerBot

# Python dependencies in their own cacheable layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The YouTube bridge tracks youtubei.js main, so keep npm in its own layer that
# a cache clean can drop without rebuilding everything above it.
COPY youtube_bridge/package.json youtube_bridge/package.json
RUN npm install --prefix youtube_bridge --omit=dev

# Everything below rebuilds on every image build.
ARG CACHEBUST=1
RUN pip install --no-cache-dir -U -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh run_bot.sh youtube_services.sh \
    && chown -R streamer:streamer /home/streamer/StreamerBot \
    && chmod -R 775 .

USER streamer

RUN python tools/compile_locales.py

ENTRYPOINT ["./entrypoint.sh"]
CMD ["./run_bot.sh", "-c", "data/config.json", "--cache", "data/StreamerBotCache.dat", "--log", "data/StreamerBot.log"]
