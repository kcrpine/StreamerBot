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
        # A restarted container keeps its own /tmp, so the lock file and socket
        # from the previous run are still sitting there and Xvfb refuses to start
        # with "Server is already active for display 99". Nothing is actually
        # listening, so Chrome then died with "Missing X server or $DISPLAY" and
        # every Netflix, Disney Plus, Apple Music and Amazon Music sign-in failed
        # at launch — on a container that had merely been restarted once.
        #
        # Only cleared when no X server actually answers, so a display that is
        # genuinely in use is never pulled out from under it.
        display_number="${DISPLAY#:}"
        display_number="${display_number%%.*}"
        if ! xdpyinfo -display "$DISPLAY" > /dev/null 2>&1; then
            rm -f "/tmp/.X${display_number}-lock" "/tmp/.X11-unix/X${display_number}"
        fi

        Xvfb "$DISPLAY" -screen 0 1280x720x24 -nolisten tcp > /tmp/xvfb.log 2>&1 &

        display_ready=0
        for _ in $(seq 1 25); do
            if xdpyinfo -display "$DISPLAY" > /dev/null 2>&1; then
                display_ready=1
                break
            fi
            sleep 0.2
        done

        # This used to print OK unconditionally, so it reported success after
        # failing all 25 checks. A start-up line that is always OK is worse than
        # no line at all: it actively points the reader away from the fault.
        if [ "$display_ready" = "1" ]; then
            echo "OK. Virtual display $DISPLAY started for the browser engine."
        else
            echo "Error. The virtual display $DISPLAY did not start, so Netflix, Disney Plus, Apple Music and Amazon Music cannot sign in or play."
            echo "Error. Xvfb said:"
            sed 's/^/  /' /tmp/xvfb.log 2>/dev/null | head -5
        fi
    else
        echo "Warning. Xvfb is missing, so the browser services cannot start."
    fi
else
    echo "Warning. Google Chrome is not in this image, so Netflix, Disney Plus, Apple Music and Amazon Music are unavailable. This is expected on arm64."
fi

exec "$@"
