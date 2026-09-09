#!/bin/bash
set -e

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
load-module module-null-sink sink_name=TTMediaBotSink rate=48000 channels=2 sink_properties=device.description="TTMediaBot_Audio_Sink"
set-default-sink TTMediaBotSink
set-default-source TTMediaBotSink.monitor
EOF

pulseaudio -D --exit-idle-time=-1

if ! pactl info > /dev/null 2>&1; then
    echo "PulseAudio failed to start."
fi

echo "PulseAudio started successfully."

exec "$@"
