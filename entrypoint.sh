#!/bin/bash
set -e

# ---------------------------------------------------------------------------
# PulseAudio.
#
# One null sink, StreamerBotSink, is the whole audio topology. mpv, Chrome and
# go-librespot all play into it, and the TeamTalk SDK captures from its monitor
# source. Exactly one engine plays at a time, so they never collide.
#
# A single sink is deliberate: bot/sound_devices.py selects the TeamTalk input
# device by list index, so adding sinks would shift those indices and silently
# break the device selection in every existing bot's config.json.
# ---------------------------------------------------------------------------
rm -rf /tmp/pulseaudio*
rm -rf ~/.config/pulse
rm -rf ~/.pulse

mkdir -p ~/.config/pulse
cat << 'EOF' > ~/.config/pulse/daemon.conf
default-sample-format = s16le
default-sample-rate = 48000
alternate-sample-rate = 48000
default-sample-channels = 2
default-channel-map = front-left,front-right
resample-method = speex-float-3
high-priority = no
realtime-scheduling = no
default-fragments = 8
default-fragment-size-msec = 25
EOF

cat << 'EOF' > ~/.config/pulse/default.pa
.include /etc/pulse/default.pa
load-module module-null-sink sink_name=StreamerBotSink rate=48000 channels=2 sink_properties=device.description="StreamerBot_Audio_Sink"
set-default-sink StreamerBotSink
set-default-source StreamerBotSink.monitor
EOF

pulseaudio -D --exit-idle-time=-1

if pactl info > /dev/null 2>&1; then
    echo "OK. PulseAudio started."
else
    echo "Error. PulseAudio failed to start. Playback will not be audible."
fi

# ---------------------------------------------------------------------------
# Xvfb.
#
# Chrome has to run headful to produce audio at all, and Netflix blocks headless
# Chrome outright, so it needs a display even though nobody ever looks at it.
# Skipped on architectures where Chrome is not installed (arm64).
# ---------------------------------------------------------------------------
export DISPLAY="${DISPLAY:-:99}"

if [ "$(cat /etc/streamerbot-browser-available 2>/dev/null || echo 0)" = "1" ]; then
    if command -v Xvfb > /dev/null 2>&1; then
        Xvfb "$DISPLAY" -screen 0 1280x720x24 -nolisten tcp > /dev/null 2>&1 &
        for _ in $(seq 1 25); do
            if xdpyinfo -display "$DISPLAY" > /dev/null 2>&1; then break; fi
            sleep 0.2
        done
        echo "OK. Virtual display $DISPLAY started for the browser engine."
    else
        echo "Warning. Xvfb is missing, so the browser services cannot start."
    fi
else
    echo "Warning. Google Chrome is not in this image, so Netflix, Disney Plus, Apple Music and Amazon Music are unavailable. This is expected on arm64."
fi

exec "$@"
